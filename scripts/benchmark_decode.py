#!/usr/bin/env python3
"""Measure decode tps for an agent, through the real server path.

Method: two sync calls per rep, differenced.

    A: max_tokens=1  -> wall_A = prefill + 1 decoded token
    B: max_tokens=N  -> wall_B = prefill + N decoded tokens
    decode_tps = (tokens_B - tokens_A) / (wall_B - wall_A)

Subtracting cancels prefill, so what is left is pure decode. Token counts come
from llama.cpp's own `usage`, not from counting SSE chunks.

Why not just time the stream? Because the server does not incrementally stream
non-thinking models. ThinkingStripper buffers every token until it sees
</think>, and a Coder-Instruct model never emits one, so the whole response
arrives as ONE chunk at flush() time. Counting chunks yielded decode tps 0.00
for every 480B run on 2026-07-14. --probe-stream re-checks that assertion and
reports TTFT.

Each call is prefixed with a unique nonce: llama-server caches prompt prefixes,
and a cached prefill on call B would make wall_B - wall_A meaningless.

Usage: python3 benchmark_decode.py [-a architect] [--max-tokens 200] [--reps 3]
"""
import argparse
import json
import os
import statistics
import time
import uuid

import requests

PROMPT = (
    "Write a Python function that computes the longest common subsequence "
    "of two strings using dynamic programming. Include type hints, a brief "
    "docstring, and one usage example. Be thorough."
)


def call(server, headers, agent, max_tokens, temperature, stream=False):
    """One completion. Returns (wall_seconds, completion_tokens, prompt_tokens, text)."""
    payload = {
        "model": agent,
        # Nonce first, so a cached prefix cannot cover this prompt.
        "messages": [{"role": "user", "content": f"[{uuid.uuid4().hex[:8]}] {PROMPT}"}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    t0 = time.time()
    r = requests.post(f"{server}/v1/chat/completions",
                      headers=headers, json=payload, timeout=900)
    r.raise_for_status()
    wall = time.time() - t0
    body = r.json()
    usage = body.get("usage") or {}
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return wall, usage.get("completion_tokens", 0), usage.get("prompt_tokens", 0), text


def probe_stream(server, headers, agent, max_tokens, temperature):
    """Time to first byte of content, and how many chunks the server really sent."""
    payload = {
        "model": agent,
        "messages": [{"role": "user", "content": f"[{uuid.uuid4().hex[:8]}] {PROMPT}"}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    t0 = time.time()
    ttft = None
    chunks = 0
    with requests.post(f"{server}/v1/chat/completions", headers=headers,
                       json=payload, stream=True, timeout=900) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "progress":   # server's own prefill event
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {})
            if delta.get("content"):
                if ttft is None:
                    ttft = time.time() - t0
                chunks += 1
    return ttft, chunks, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--agent", default="architect")
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--reps", type=int, default=3, help="median is reported")
    ap.add_argument("--server", default="http://127.0.0.1:5000")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--probe-stream", action="store_true",
                    help="also report TTFT and flag non-incremental streaming")
    args = ap.parse_args()

    headers = {"Content-Type": "application/json"}
    if (k := os.getenv("ADMIN_API_KEY")):
        headers["Authorization"] = f"Bearer {k}"

    print(f"agent: {args.agent}   max_tokens: {args.max_tokens}   reps: {args.reps}")

    # Untimed: pays the on-demand model load, and decode climbs over a session's
    # first runs (sweep_cpu_moe measured 65 -> 75 tok/s).
    print("warming up (loads the model on demand; a 480B takes ~80s)...", flush=True)
    call(args.server, headers, args.agent, 8, args.temperature)

    rows = []
    for i in range(1, args.reps + 1):
        wall_a, tok_a, n_prompt, _ = call(
            args.server, headers, args.agent, 1, args.temperature)
        wall_b, tok_b, _, text = call(
            args.server, headers, args.agent, args.max_tokens, args.temperature)

        d_wall, d_tok = wall_b - wall_a, tok_b - tok_a
        if d_wall <= 0 or d_tok <= 0:
            print(f"  rep {i}: UNUSABLE (dwall={d_wall:.2f}s dtok={d_tok}) — "
                  f"model likely swapped or prompt cache hit")
            continue
        tps = d_tok / d_wall
        rows.append({"tps": tps, "prefill_wall": wall_a, "n_prompt": n_prompt,
                     "tok_b": tok_b, "text": text})
        print(f"  rep {i}: decode {tps:6.2f} tok/s   "
              f"(prefill {wall_a:5.2f}s over {n_prompt} prompt tok | "
              f"{d_tok} tok in {d_wall:.2f}s)", flush=True)

    if not rows:
        print("\nno usable reps.")
        return

    print(f"\ndecode tps:   {statistics.median(r['tps'] for r in rows):.2f} tok/s"
          f"   (median of {len(rows)})")
    print(f"prefill wall: {statistics.median(r['prefill_wall'] for r in rows):.2f}s"
          f"   over {rows[0]['n_prompt']} prompt tokens")

    if args.probe_stream:
        ttft, chunks, total = probe_stream(
            args.server, headers, args.agent, args.max_tokens, args.temperature)
        # ttft is None when no content delta ever arrived — the degenerate case
        # the warning below exists for, so it must not crash the format.
        ttft_s = f"{ttft:.2f}s" if ttft is not None else "n/a"
        print(f"\nstream probe: TTFT {ttft_s} | {chunks} content chunks | "
              f"total {total:.2f}s")
        if chunks <= 1:
            print("  WARNING: the server sent the whole response as one chunk. It is not\n"
                  "  streaming this model — ThinkingStripper buffers until </think>, which a\n"
                  "  non-thinking model never emits, so nothing reaches the client until the\n"
                  "  generation ends. Decode above is still correct (it does not use the stream).")

    print("\n--- output preview (first 200 chars) ---")
    print(rows[-1]["text"][:200])


if __name__ == "__main__":
    main()
