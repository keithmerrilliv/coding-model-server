#!/usr/bin/env python3
"""End-to-end smoke test for native tool_calls against llama-server.

Spins up llama-server on a private port with GLM-4.7-Flash + --jinja, then
runs a 2-turn conversation that exercises the OpenAI tool-calling shape:

  turn 1: user asks "list /tmp"; we send tools=[remote_exec] → expect
          assistant message with tool_calls (no marker text)
  turn 2: dispatch shell command → send back role="tool" with stdout →
          expect a final assistant message that references the listing

If both turns behave correctly the wire is proven. The coding-model-server proxy
in server.py just passes these chunks through unchanged.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
LLAMA_SERVER = REPO / "tools" / "llama-server"
LIBS = REPO / "tools"
MODEL = Path("/home/keith-merrill/.lmstudio/models/unsloth/GLM-4.7-Flash-GGUF/GLM-4.7-Flash-Q4_K_M.gguf")
PORT = int(os.environ.get("SMOKE_PORT", "8082"))
LOG = Path("/tmp/native-tools-smoke.log")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "remote_exec",
        "description": "Execute a shell command on the user's machine and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
            },
            "required": ["command"],
        },
    },
}]


def start_server():
    cmd = [
        str(LLAMA_SERVER), "-m", str(MODEL),
        "-ngl", "47", "-c", "16384", "-b", "2048", "-ub", "2048",
        "-t", "24", "-tb", "32", "-fa", "auto", "--mmap",
        "--cache-type-k", "q4_0", "--cache-type-v", "q4_0",
        "--host", "127.0.0.1", "--port", str(PORT), "-np", "1",
        "--cpu-moe", "--jinja", "--reasoning-format", "none",
    ]
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{LIBS}:{env.get('LD_LIBRARY_PATH', '')}"
    log = LOG.open("w")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    print(f"[smoke] llama-server started PID={proc.pid}, waiting for /health…")
    deadline = time.time() + 240
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"[smoke] llama-server died during startup; tail of log:\n{LOG.read_text()[-2000:]}")
        try:
            r = requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2)
            if r.status_code == 200:
                print(f"[smoke] healthy")
                return proc
        except requests.RequestException:
            pass
        time.sleep(2)
    proc.kill()
    sys.exit("[smoke] healthcheck timed out")


def stream_and_collect(messages):
    """POST a streaming completion. Returns (assistant_message_dict, finish_reason).

    The assistant_message_dict has the OpenAI shape: {role, content, tool_calls?}
    with tool_calls reassembled from indexed deltas.
    """
    payload = {
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "temperature": 0.2,
        "max_tokens": 600,
        "stream": True,
    }
    r = requests.post(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                      json=payload, stream=True, timeout=120)
    if r.status_code != 200:
        sys.exit(f"[smoke] llama-server returned {r.status_code}: {r.text}")

    content_parts = []
    calls = {}  # index -> {id, name, args}
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
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta", {})
        if delta.get("content"):
            content_parts.append(delta["content"])
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

    msg = {"role": "assistant", "content": "".join(content_parts) or None}
    if calls:
        msg["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["args"]}}
            for _, c in sorted(calls.items())
        ]
    return msg, finish_reason


def run_tool(name, args_json):
    """Minimal local dispatcher — no permission prompts, just for the smoke test."""
    if name != "remote_exec":
        return f"unknown tool: {name}"
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as e:
        return f"bad json args: {e}"
    cmd = args.get("command", "")
    print(f"[smoke] dispatching: {cmd!r}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    out = proc.stdout
    if proc.stderr:
        out = (out + "\n[stderr]\n" + proc.stderr).strip()
    return out or "(no output)"


def main():
    proc = start_server()
    try:
        messages = [
            {"role": "system",
             "content": "You are a helpful assistant. Use the remote_exec tool to answer questions about the user's filesystem. Keep replies short."},
            {"role": "user",
             "content": "How many entries in /tmp start with 'native-tools'? One short sentence."},
        ]

        MAX_TURNS = 4
        saw_tool_calls = False
        for turn in range(1, MAX_TURNS + 1):
            print(f"\n[smoke] === turn {turn} ===")
            msg, fr = stream_and_collect(messages)
            print(f"[smoke] finish_reason={fr}")
            print(f"[smoke] content={(msg.get('content') or '')[:300]!r}")
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                print(f"[smoke] tool_calls={json.dumps(tool_calls, indent=2)}")

            content = msg.get("content") or ""
            for marker in ("<<<REMOTE_EXEC>>>", "<REMOTE_EXEC>"):
                assert marker not in content, f"marker {marker} leaked into content alongside native tool_calls"

            messages.append(msg)

            if fr == "tool_calls":
                assert tool_calls, "finish_reason=tool_calls but no tool_calls reassembled"
                saw_tool_calls = True
                for tc in tool_calls:
                    result = run_tool(tc["function"]["name"], tc["function"]["arguments"])
                    print(f"[smoke] tool result: {result[:200]!r}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": result,
                    })
                continue

            if fr == "stop":
                assert saw_tool_calls, "model never called a tool"
                assert msg.get("content"), "final assistant turn had no content"
                print(f"\n[smoke] PASSED ✓ (resolved in {turn} turns)")
                return

            sys.exit(f"[smoke] unexpected finish_reason={fr!r}")

        sys.exit(f"[smoke] FAILED — model did not converge within {MAX_TURNS} turns")
    finally:
        print("[smoke] stopping llama-server…")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
