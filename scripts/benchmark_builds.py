#!/usr/bin/env python3
"""A/B benchmark: compare llama-server builds on an identical model + flags.

Runs each build standalone on a scratch port with the same config, sends one
fixed prompt, and reports llama-server's own prefill/decode tok/s plus a sample
of the output — the sample is a coherence check, since a mismatched CUDA build
can produce fast MMQ garbage rather than an honest failure.

The original version of this script hardcoded two sibling llama.cpp build dirs
(build-cu128, build-cu132) that no longer exist, so it could not run at all.
Builds are now arguments and missing ones are skipped with a warning:

    ./venv/bin/python scripts/benchmark_builds.py
    ./venv/bin/python scripts/benchmark_builds.py \
        --build cu128=/home/youruser/Dev/llama.cpp/build-cu128/bin

With no --build, this just measures the current production binary — useful as a
baseline, but the comparison is the point, so pass at least one alternate.

Historical note: the 2026-06-03 run of this concluded a rebuild was a wash
(these models are CPU-MoE-bound); the real lever was --n-cpu-moe offload. See
scripts/sweep_cpu_moe.py.
"""
import argparse
import os
import sys

from _llama_bench import PORT, PROMPT, TOOLS, free_gpu, measure, restore_server

DEFAULT_MODEL = (
    "/home/youruser/.lmstudio/models/unsloth/"
    "Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
)
# Identical across builds — the point is to vary only the binary.
FLAGS = ["-ngl", "49", "-c", "8192", "-b", "3584", "-ub", "3584",
         "-t", "24", "-tb", "24", "-fa", "auto", "--mmap",
         "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
         "--host", "127.0.0.1", "--port", str(PORT), "-np", "1",
         "--cpu-moe", "--chat-template", "chatml"]


def parse_build(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"expected NAME=DIR, got {spec!r}")
    name, _, path = spec.partition("=")
    return name, os.path.expanduser(path)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", action="append", type=parse_build, default=[],
                   metavar="NAME=DIR",
                   help="extra build to compare; DIR holds llama-server + its .so")
    p.add_argument("--model", default=DEFAULT_MODEL, help="GGUF to load")
    p.add_argument("--n-predict", type=int, default=256)
    args = p.parse_args()

    if not os.path.isfile(args.model):
        print(f"model not found: {args.model}", file=sys.stderr)
        return 2

    builds = [("current", TOOLS)] + args.build
    live = []
    for name, libdir in builds:
        if os.path.isfile(os.path.join(libdir, "llama-server")):
            live.append((name, libdir))
        else:
            print(f"skipping {name}: no llama-server in {libdir}", flush=True)
    if not live:
        print("no runnable builds", file=sys.stderr)
        return 2
    if len(live) == 1:
        print("note: only one build — measuring a baseline, not comparing.", flush=True)

    print("freeing GPU (killing the live llama-server child)...", flush=True)
    free_gpu()

    results = []
    try:
        for name, libdir in live:
            print(f"\n=== {name} ({libdir}) ===", flush=True)
            res = measure(FLAGS + ["-m", args.model],
                          binary=os.path.join(libdir, "llama-server"), libdir=libdir,
                          prompt=PROMPT, npred=args.n_predict, log_tag=name)
            print(res, flush=True)
            results.append((name, res))
    finally:
        restore_server()

    print("\n" + "=" * 74)
    print(f"{'build':<18}{'prefill t/s':>13}{'decode t/s':>13}   first output tokens")
    for name, r in results:
        if "error" in r:
            print(f"{name:<18}  ERROR: {r['error']}")
            continue
        print(f"{name:<18}{(r['prefill_tps'] or 0):>13.1f}{(r['decode_tps'] or 0):>13.2f}"
              f"   {r['sample'][:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
