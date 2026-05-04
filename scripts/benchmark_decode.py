#!/usr/bin/env python3
"""Measure decode tps for an agent.

Sends a small prompt, asks for a long generation, times from first to last
streamed token. Run this both with and without spec decode to A/B.

Usage: python3 benchmark_decode.py [-a architect] [--max-tokens 200]
"""
import argparse
import json
import os
import time

import requests


PROMPT = (
    "Write a Python function that computes the longest common subsequence "
    "of two strings using dynamic programming. Include type hints, a brief "
    "docstring, and one usage example. Be thorough."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--agent", default="architect")
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--server", default="http://127.0.0.1:5000")
    ap.add_argument("--temperature", type=float, default=0.7)
    args = ap.parse_args()

    headers = {"Content-Type": "application/json"}
    if (k := os.getenv("ADMIN_API_KEY")):
        headers["Authorization"] = f"Bearer {k}"

    payload = {
        "model": args.agent,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "stream": True,
    }

    t_request = time.time()
    t_first = None
    t_last = None
    n_tokens = 0
    text_chunks = []

    with requests.post(
        f"{args.server}/v1/chat/completions",
        headers=headers, json=payload, stream=True, timeout=600,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = obj.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if content:
                if t_first is None:
                    t_first = time.time()
                t_last = time.time()
                n_tokens += 1
                text_chunks.append(content)

    if t_first is None:
        print("No tokens received.")
        return

    ttft = t_first - t_request
    decode_wall = (t_last - t_first) if t_last and n_tokens > 1 else 0.0
    decode_tps = (n_tokens - 1) / decode_wall if decode_wall > 0 else 0.0

    print(f"agent:        {args.agent}")
    print(f"prompt chars: {len(PROMPT)}")
    print(f"chunks:       {n_tokens}  (chunks ≈ tokens, but tokenizer may merge)")
    print(f"TTFT:         {ttft:.2f}s")
    print(f"decode wall:  {decode_wall:.2f}s")
    print(f"decode tps:   {decode_tps:.2f} chunks/s")
    print(f"total wall:   {time.time() - t_request:.2f}s")
    print()
    print("--- output preview (first 200 chars) ---")
    print("".join(text_chunks)[:200])


if __name__ == "__main__":
    main()
