#!/usr/bin/env python3
"""Validate that the coding-model-server proxy forwards tools and tool_calls.

Single-turn test against the running coding-model-server (port 5000) that:
  1. POSTs /v1/chat/completions with model='native_implementer', tools=[remote_exec]
  2. Reads the SSE stream, verifies delta.tool_calls chunks arrive
  3. Reassembles into OpenAI-shape and prints the result

This proves the server-side changes only — the orchestrator integration
needs an interactive client run (CODING_MODEL_NATIVE_TOOLS=1 ./client.py --model native_implementer).
"""
import json
import os
import sys
from pathlib import Path

import requests

# Load admin key from .env
env_path = Path(__file__).resolve().parent.parent / ".env"
admin_key = ""
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith("ADMIN_API_KEY="):
            admin_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
admin_key = os.environ.get("ADMIN_API_KEY") or admin_key
if not admin_key:
    sys.exit("[proxy-test] ADMIN_API_KEY not found in .env or env")

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
    "messages": [
        # No client system message — let the server inject its tools-aware
        # agent prompt (system_prompt_native_tools). That prompt teaches the
        # model to call remote_exec via the function-call interface and
        # reserve marker tools for file operations.
        {"role": "user",
         "content": "How many entries in /tmp start with 'native-tools'? One short sentence."},
    ],
    "tools": TOOLS,
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "temperature": 0.2,
    "max_tokens": 4000,
    "stream": True,
}

print(f"[proxy-test] POST {URL} model=native_implementer tools=[remote_exec]")
r = requests.post(URL, json=payload, headers={"X-Admin-Key": admin_key},
                  stream=True, timeout=600)
if r.status_code != 200:
    sys.exit(f"[proxy-test] HTTP {r.status_code}: {r.text[:500]}")

content_parts = []
calls = {}
finish_reason = None
saw_tool_call_delta = False
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
        print(f"[proxy-test] progress: prefill {chunk['prompt_tokens']}/{chunk['n_ctx']} tokens")
        continue
    if "error" in chunk:
        sys.exit(f"[proxy-test] server error: {chunk['error']}")
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta", {})
    if delta.get("content"):
        content_parts.append(delta["content"])
    for tc in delta.get("tool_calls") or []:
        saw_tool_call_delta = True
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

print(f"\n[proxy-test] finish_reason={finish_reason!r}")
print(f"[proxy-test] saw_tool_call_delta={saw_tool_call_delta}")
print(f"[proxy-test] content (post-strip)={''.join(content_parts)!r}")
for idx in sorted(calls):
    c = calls[idx]
    print(f"[proxy-test] tool_call[{idx}] name={c['name']!r} args={c['args']!r} id={c['id'][:12]!r}")

if not saw_tool_call_delta:
    sys.exit("[proxy-test] FAILED — no tool_calls deltas proxied through coding-model-server")
if finish_reason != "tool_calls":
    sys.exit(f"[proxy-test] FAILED — expected finish_reason=tool_calls, got {finish_reason!r}")
if not calls:
    sys.exit("[proxy-test] FAILED — no calls reassembled")

print("\n[proxy-test] PASSED ✓ — coding-model-server proxies native tool_calls correctly")
