#!/usr/bin/env python3
"""Benchmark prefill speed for all configured agents.

Sends a fixed-size prompt to each agent and measures time-to-first-token (TTFT)
and prefill throughput (prompt tokens / TTFT).  Designed to run on the server
machine for accurate measurements.

Usage:
    python3 benchmark_prefill.py                       # benchmark all agents
    python3 benchmark_prefill.py -a dense_architect native_implementer  # benchmark specific agents
    python3 benchmark_prefill.py --prompt-tokens 4000  # custom prompt size
    python3 benchmark_prefill.py --warmup              # discard first request
    python3 benchmark_prefill.py --json results.json   # save results

Environment:
    CODING_MODEL_SERVER_IP    default 127.0.0.1
    CODING_MODEL_SERVER_PORT  default 5000
    ADMIN_API_KEY     required if server has auth enabled
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests


# Lorem-ipsum-style filler — gives the tokenizer real work without being
# pathologically repetitive (which could trigger prefix caching artifacts).
_FILLER = (
    "The quick brown fox jumps over the lazy dog while contemplating "
    "the nature of recursion in functional programming languages. "
    "Distributed systems require careful consideration of consistency, "
    "availability, and partition tolerance — the classic CAP theorem trade-off. "
    "In Swift, value types and reference types behave differently with respect "
    "to mutation, copying, and identity. Memory ordering in concurrent code is "
    "subtle: acquire, release, sequentially consistent, and relaxed orderings "
    "each have specific use cases that must be matched to the algorithm. "
)


def build_prompt(target_tokens):
    """Build a prompt of approximately *target_tokens* tokens.

    Uses the server's 3.5 chars/token estimate.  Returns a string slightly
    larger than target so the actual measured token count meets or exceeds it.
    """
    target_chars = int(target_tokens * 3.8)
    repeats = (target_chars // len(_FILLER)) + 1
    return (_FILLER * repeats)[:target_chars]


def benchmark_agent(server_url, headers, agent, prompt, max_tokens=4, timeout=1800):
    """Send a streaming completion and measure prefill performance.

    Returns dict with: agent, prompt_tokens, ttft, prefill_rate, error.
    Stops streaming as soon as the first content token arrives.
    """
    payload = {
        "model": agent,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }

    result = {
        "agent": agent,
        "prompt_tokens": None,
        "ttft": None,
        "prefill_rate": None,
        "error": None,
    }

    start = time.time()
    try:
        with requests.post(server_url, json=payload, headers=headers,
                           stream=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                return result

            first_token_time = None
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # Server emits a progress event with the actual token count
                if chunk.get("type") == "progress":
                    result["prompt_tokens"] = chunk.get("prompt_tokens")
                    continue

                choices = chunk.get("choices", [])
                if choices and choices[0].get("delta", {}).get("content"):
                    if first_token_time is None:
                        first_token_time = time.time()
                        break  # have what we need

            if first_token_time:
                result["ttft"] = first_token_time - start
                if result["prompt_tokens"]:
                    result["prefill_rate"] = result["prompt_tokens"] / result["ttft"]
            else:
                result["error"] = "No content tokens received"
    except requests.exceptions.Timeout:
        result["error"] = f"Timed out after {timeout}s"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def get_available_agents(host, port, headers):
    url = f"http://{host}:{port}/v1/models"
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]


def print_table(results):
    """Render results sorted slowest-first (excluding errors at the bottom)."""
    ok = [r for r in results if r["error"] is None]
    err = [r for r in results if r["error"] is not None]
    ok.sort(key=lambda r: r["prefill_rate"] or 0)  # slowest first

    print()
    print("| Rank | Agent | Prompt tokens | TTFT (s) | Prefill (tok/s) |")
    print("|------|-------|---------------|----------|-----------------|")
    for i, r in enumerate(ok, 1):
        print(f"| {i} | `{r['agent']}` | {r['prompt_tokens']:,} | "
              f"{r['ttft']:.1f} | **{r['prefill_rate']:.1f}** |")
    if err:
        print()
        print("Errors:")
        for r in err:
            print(f"  - `{r['agent']}`: {r['error']}")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-a", "--agents", nargs="+",
                        help="Specific agents to benchmark (default: all)")
    parser.add_argument("--prompt-tokens", type=int, default=4000,
                        help="Approximate prompt size in tokens (default: 4000)")
    parser.add_argument("--warmup", action="store_true",
                        help="Run a small warmup request before measuring (loads model)")
    parser.add_argument("--json", help="Write results to JSON file")
    parser.add_argument("--host", default=os.getenv("CODING_MODEL_SERVER_IP", "127.0.0.1"))
    parser.add_argument("--port", default=os.getenv("CODING_MODEL_SERVER_PORT", "5000"))
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Per-request timeout in seconds (default: 1800)")
    args = parser.parse_args()

    server_url = f"http://{args.host}:{args.port}/v1/chat/completions"
    api_key = os.getenv("ADMIN_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Admin-Key"] = api_key

    print(f"Server:       http://{args.host}:{args.port}")
    print(f"Auth:         {'X-Admin-Key' if api_key else 'none'}")
    print(f"Prompt size:  ~{args.prompt_tokens:,} tokens")
    print(f"Warmup:       {args.warmup}")
    print()

    if args.agents:
        agents = args.agents
    else:
        try:
            agents = get_available_agents(args.host, args.port, headers)
            print(f"Discovered {len(agents)} agents")
        except Exception as e:
            print(f"Failed to fetch agent list: {e}", file=sys.stderr)
            sys.exit(1)

    prompt = build_prompt(args.prompt_tokens)
    print(f"Prompt:       {len(prompt):,} chars")
    print()

    results = []
    for i, agent in enumerate(agents, 1):
        print(f"[{i}/{len(agents)}] {agent:25s}", end=" ", flush=True)

        if args.warmup:
            # Discard warmup result — first request triggers model load
            benchmark_agent(server_url, headers, agent, _FILLER, max_tokens=2,
                            timeout=args.timeout)

        result = benchmark_agent(server_url, headers, agent, prompt,
                                 timeout=args.timeout)
        results.append(result)

        if result["error"]:
            print(f"ERROR: {result['error'][:80]}")
        else:
            print(f"{result['prompt_tokens']:>6,} tok / "
                  f"{result['ttft']:>6.1f}s = {result['prefill_rate']:>7.1f} tok/s")

    print_table(results)

    if args.json:
        out = {
            "timestamp": datetime.now().isoformat(),
            "host": f"http://{args.host}:{args.port}",
            "prompt_tokens_target": args.prompt_tokens,
            "warmup": args.warmup,
            "results": results,
        }
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Results written to {args.json}")


if __name__ == "__main__":
    main()
