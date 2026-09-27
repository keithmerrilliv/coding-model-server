#!/usr/bin/env python3
"""Verify the hybrid prompt: native remote_exec + marker file tools coexist.

Asks the model to (a) check git status and (b) write a small file. We expect
the model to call `remote_exec` natively for git AND emit a `<<<WRITE_FILE>>>`
marker for the file write — both in the same response.
"""
import json
import os
import sys
from pathlib import Path

import requests

env_path = Path(__file__).resolve().parent.parent / ".env"
admin_key = os.environ.get("ADMIN_API_KEY") or ""
if not admin_key and env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith("ADMIN_API_KEY="):
            admin_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
if not admin_key:
    sys.exit("[hybrid] ADMIN_API_KEY missing")

URL = "http://127.0.0.1:5000/v1/chat/completions"
TOOLS = [{
    "type": "function",
    "function": {
        "name": "remote_exec",
        "description": "Execute a shell command and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]

payload = {
    "model": "native_implementer",
    "messages": [{
        "role": "user",
        "content": (
            "Two tasks: (1) run `git status --short` to show what's pending, "
            "(2) create a file at /tmp/native_tools_hybrid_demo.txt containing "
            "the single line 'hybrid test ok'. Do both."
        ),
    }],
    "tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": True,
    "temperature": 0.2, "max_tokens": 4000, "stream": True,
}

print(f"[hybrid] POST {URL}")
r = requests.post(URL, json=payload, headers={"X-Admin-Key": admin_key},
                  stream=True, timeout=600)
if r.status_code != 200:
    sys.exit(f"[hybrid] HTTP {r.status_code}: {r.text[:500]}")

content = []
calls = {}
finish_reason = None
for line in r.iter_lines(decode_unicode=True):
    if not line or not line.startswith("data: "):
        continue
    data = line[6:]
    if data.strip() == "[DONE]":
        break
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        continue
    if chunk.get("type") == "progress":
        print(f"[hybrid] prefill {chunk['prompt_tokens']} tokens")
        continue
    if "error" in chunk:
        sys.exit(f"[hybrid] server error: {chunk['error']}")
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta", {})
    if delta.get("content"):
        content.append(delta["content"])
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
        if "id" in tc:
            slot["id"] = tc["id"]
        fn = tc.get("function", {})
        if "name" in fn:
            slot["name"] += fn["name"]
        if "arguments" in fn:
            slot["args"] += fn["arguments"]
    if choice.get("finish_reason"):
        finish_reason = choice["finish_reason"]

text = "".join(content)
print(f"\n[hybrid] finish_reason={finish_reason!r}")
print(f"[hybrid] content (post-strip, {len(text)} chars):\n---\n{text}\n---")
for idx in sorted(calls):
    c = calls[idx]
    print(f"[hybrid] tool_call[{idx}] {c['name']}({c['args']})")

has_native = bool(calls) and any(c["name"] == "remote_exec" for c in calls.values())
has_marker_write = "<<<WRITE_FILE>>>" in text or "<WRITE_FILE>" in text

print()
print(f"[hybrid] native remote_exec emitted:    {has_native}")
print(f"[hybrid] <<<WRITE_FILE>>> marker emitted: {has_marker_write}")

if has_native and has_marker_write:
    print("[hybrid] PASSED ✓ — both conventions coexist in one response")
elif has_native:
    print("[hybrid] PARTIAL — native fired, marker did not. Model may have")
    print("                   chosen to use remote_exec for the file write")
    print("                   (against the system-prompt rule).")
elif has_marker_write:
    print("[hybrid] PARTIAL — marker fired, native did not.")
else:
    print("[hybrid] FAIL — neither tool convention triggered")
