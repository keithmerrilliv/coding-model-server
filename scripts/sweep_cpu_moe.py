#!/usr/bin/env python3
"""Sweep --n-cpu-moe per agent to pick each model's expert-offload point.

`--cpu-moe` keeps ALL expert weights on CPU — that is the decode bottleneck on
these MoE models. `--n-cpu-moe N` keeps only the first N layers' experts on CPU
and runs the rest on the GPU: lower N => more experts on the GPU => faster
decode, until VRAM OOMs. This sweeps N high->low, records decode tok/s and
resident VRAM at each point, stops at the first failure to load, and recommends
the lowest N that still leaves a safe headroom margin.

Each config is launched with the agent's REAL production argv (via
LlamaServerManager._build_server_args), with only the port and the offload flag
substituted. The previous version hand-copied each model's ngl/ubatch into a
literal table, which drifted out of sync with config.py the moment a model
changed — the sweep was then measuring flags nobody actually runs.

    ./venv/bin/python scripts/sweep_cpu_moe.py                     # implementer
    ./venv/bin/python scripts/sweep_cpu_moe.py -a implementer -a reviewer
    ./venv/bin/python scripts/sweep_cpu_moe.py -a implementer --ctx 32768

Kills the live server's llama-server child to free the GPU; the FastAPI server
stays up and respawns it on the next real request.
"""
import argparse
import sys

from _llama_bench import (
    BINARY, PORT, free_gpu, gpu_total, measure, restore_server, set_flag,
    strip_moe_flags,
)

from coding_model_server.config import Config
from coding_model_server.llama_server import LlamaServerManager

DEFAULT_N_VALUES = [44, 40, 36, 32, 28, 26, 24, 22, 20, 18]
DEFAULT_SAFE_FREE_MIB = 1400


def agent_flags(agent: str, ctx: int | None) -> tuple[str, list[str]]:
    """Production argv for `agent`, port (and optionally ctx) overridden."""
    model_config = Config.AGENTS[agent]["model_config"]
    argv = LlamaServerManager()._build_server_args(BINARY, model_config)
    binary, flags = argv[0], argv[1:]
    flags = set_flag(flags, "--port", str(PORT))
    if ctx is not None:
        flags = set_flag(flags, "-c", str(ctx))
    return binary, flags


def sweep(agent: str, n_values: list[int], ctx: int | None,
          safe_free: int, total_mib: int) -> None:
    binary, base_flags = agent_flags(agent, ctx)
    stripped = strip_moe_flags(base_flags)
    eff_ctx = base_flags[base_flags.index("-c") + 1]
    print(f"\n########## {agent} — {Config.AGENTS[agent]['description']}", flush=True)
    print(f"########## ctx={eff_ctx}  (production argv, offload flag swept)\n", flush=True)

    base = measure(stripped + ["--cpu-moe"], binary=binary, log_tag=f"{agent}-cpumoe")
    print(f"  --cpu-moe baseline: {base}", flush=True)
    if "error" in base:
        print(f"  cannot establish a baseline for {agent}; skipping.", flush=True)
        return
    base_decode = base["decode_tps"]

    rows = []
    for n in n_values:
        res = measure(stripped + ["--n-cpu-moe", str(n)], binary=binary,
                      log_tag=f"{agent}-ncmoe{n}")
        print(f"  n-cpu-moe {n}: {res}", flush=True)
        if "error" in res:
            print(f"   -> {res['error']}\n   -> stopping: lower N needs even more VRAM.",
                  flush=True)
            break
        rows.append((n, res))

    safe = [(n, r) for n, r in rows if (total_mib - r["vram_mib"]) >= safe_free]
    print(f"\n  == {agent} summary (baseline --cpu-moe decode {base_decode:.2f} t/s) ==")
    print(f"  {'config':<16}{'VRAM MiB':>9}{'free':>7}{'prefill t/s':>13}{'decode t/s':>12}{'vs base':>10}")
    for n, r in rows:
        free = total_mib - r["vram_mib"]
        gain = (r["decode_tps"] / base_decode - 1) * 100 if base_decode else 0
        tag = "  <-- SAFE pick" if safe and n == safe[-1][0] else ""
        print(f"  n-cpu-moe {n:<6}{r['vram_mib']:>9}{free:>7}"
              f"{(r['prefill_tps'] or 0):>13.1f}{(r['decode_tps'] or 0):>12.2f}"
              f"{gain:>+9.0f}%{tag}")

    if safe:
        n, r = safe[-1]
        print(f"\n  RECOMMEND {agent}: n_cpu_moe={n} "
              f"(decode {r['decode_tps']:.2f} t/s, {total_mib - r['vram_mib']} MiB free)")
        print(f"  Current config.py value: n_cpu_moe="
              f"{Config.AGENTS[agent]['model_config'].get('n_cpu_moe')}")
    else:
        print(f"\n  RECOMMEND {agent}: keep --cpu-moe "
              f"(no offload point leaves {safe_free} MiB free at ctx={eff_ctx})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--agent", action="append", dest="agents",
                   help="agent to sweep (repeatable; default: implementer)")
    p.add_argument("--ctx", type=int,
                   help="override n_ctx (default: the agent's production value)")
    p.add_argument("--safe-free", type=int, default=DEFAULT_SAFE_FREE_MIB,
                   help=f"MiB of VRAM to leave free (default {DEFAULT_SAFE_FREE_MIB})")
    p.add_argument("-n", "--n-values", type=int, nargs="+", default=DEFAULT_N_VALUES,
                   help="N values to sweep, high to low")
    args = p.parse_args()

    agents = args.agents or ["implementer"]
    unknown = [a for a in agents if a not in Config.AGENTS]
    if unknown:
        print(f"unknown agent(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(Config.AGENTS)}", file=sys.stderr)
        return 2

    total = gpu_total()
    print(f"GPU total {total} MiB; freeing it (killing the live llama-server child)...",
          flush=True)
    free_gpu()

    try:
        for agent in agents:
            sweep(agent, args.n_values, args.ctx, args.safe_free, total)
    finally:
        restore_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
