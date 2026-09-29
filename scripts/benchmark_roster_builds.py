#!/usr/bin/env python3
"""DEV-903: measure every roster model on two llama-server builds, with the
exact flags production passes it.

    ./venv/bin/python scripts/benchmark_roster_builds.py \
        --new ~/Dev/llamacpp-v050/build/bin [--agents deep_reviewer ...]

For each distinct model config in ``Config.AGENTS`` (agents that share one are
measured once), each build is spawned on the scratch port with the argv
``LlamaServerManager._build_server_args`` builds for production. Only the port,
the lookup cache (a scratch file, never the live one) and slot saving (off)
differ. Each spawn is measured with:

* resident VRAM, and the free VRAM the admission check would see;
* prefill and decode tok/s on a short prompt and on a long one (~26K tokens,
  where architect and synthesis prompts live), as llama-server reports them;
* the temperature-0, fixed-seed output of the short prompt, compared across
  builds for parity.

The old build is ``tools/`` (production). The card must be free: the script
refuses to spawn while VRAM use is above --max-used-mib, and never kills the
live server's child. Unload it first, with nothing in flight.

Prints one JSON line per (model, build) and a comparison table at the end.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _llama_bench import PORT, PROMPT, TOOLS, gpu_free, gpu_used, wait_gpu_free  # noqa: E402

from coding_model_server.config import Config  # noqa: E402
from coding_model_server.llama_server import LlamaServerManager  # noqa: E402

LOOKUP_CACHE = "/tmp/llama-bench-lookup-cache.bin"
REPO = Path(__file__).resolve().parent.parent
# The long prompt: real source from this repo, so it tokenises like the code
# the pipeline sends. ~92K characters is ~26K tokens.
LONG_SOURCE = REPO / "src" / "coding_model_server" / "orchestrator_daemon.py"
LONG_CHARS = 92_000


class _Argv:
    """The attributes _build_server_args reads, with the production values
    except those a side-by-side spawn must not share with the live child."""
    LLAMA_SERVER_PORT = PORT
    CACHE_RAM_MIB = LlamaServerManager.CACHE_RAM_MIB
    SLOT_SAVE_ENABLED = False
    _CACHE_TYPE_NAMES = LlamaServerManager._CACHE_TYPE_NAMES


def production_argv(binary: str, model_config: dict) -> list[str]:
    cmd = LlamaServerManager._build_server_args(_Argv(), binary, model_config)  # type: ignore[arg-type]
    i = cmd.index("--lookup-cache-dynamic")
    cmd[i + 1] = LOOKUP_CACHE
    return cmd


def distinct_models(agents: list[str] | None) -> dict[str, tuple[list[str], dict]]:
    """model key → (agents sharing it, its config). Agents whose configs are
    identical share one measurement."""
    out: dict[str, tuple[list[str], dict]] = {}
    for name, agent in Config.AGENTS.items():
        if agents and name not in agents:
            continue
        cfg = agent["model_config"]
        key = json.dumps(cfg, sort_keys=True)
        out.setdefault(key, ([], cfg))[0].append(name)
    return out


def _complete(prompt: str, npred: int, timeout: int) -> dict:
    r = requests.post(f"http://127.0.0.1:{PORT}/completion",
                      json={"prompt": prompt, "n_predict": npred, "temperature": 0,
                            "seed": 42, "cache_prompt": False}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def measure(binary: str, libdir: str, model_config: dict, long_prompt: str,
            tag: str, npred: int, max_used: int) -> dict:
    argv = production_argv(binary, model_config)
    env = dict(os.environ, LD_LIBRARY_PATH=libdir)
    log_path = f"/tmp/llama-bench-{tag}.log"
    with open(log_path, "w") as log:
        t0 = time.monotonic()
        proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(900):
                if proc.poll() is not None:
                    return {"error": f"exited {proc.returncode}; see {log_path}"}
                try:
                    if requests.get(f"http://127.0.0.1:{PORT}/health",
                                    timeout=2).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                return {"error": f"health timeout; see {log_path}"}
            load_s = round(time.monotonic() - t0, 1)
            _complete("Hello.", 4, 300)          # warm the graph
            out = {"load_s": load_s, "vram_mib": gpu_used(), "free_mib": gpu_free()}
            for label, prompt in (("short", PROMPT), ("long", long_prompt)):
                r = _complete(prompt, npred, 1800)
                t = r.get("timings", {})
                out[label] = {"prompt_n": t.get("prompt_n"),
                              "prefill_tps": t.get("prompt_per_second"),
                              "decode_tps": t.get("predicted_per_second")}
                if label == "short":
                    out["short_output"] = r.get("content", "")
            return out
        except requests.RequestException as exc:
            return {"error": f"{type(exc).__name__}: {exc}; see {log_path}"}
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            wait_gpu_free(limit=max_used, tries=60)


def _parity(a: str, b: str) -> str:
    if a == b:
        return "identical"
    n = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return f"diverges at char {n}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--new", required=True, type=Path,
                    help="directory holding the new llama-server and its .so files")
    ap.add_argument("--agents", nargs="*", help="limit to these roster agents")
    ap.add_argument("--n-predict", type=int, default=256)
    ap.add_argument("--max-used-mib", type=int, default=1500,
                    help="refuse to spawn while the card holds more than this")
    ap.add_argument("--out", type=Path, default=Path("/tmp/benchmark_roster_builds.jsonl"))
    args = ap.parse_args()

    builds = {"old": (os.path.join(TOOLS, "llama-server"), TOOLS),
              "new": (str(args.new.expanduser() / "llama-server"),
                      str(args.new.expanduser()))}
    long_prompt = (LONG_SOURCE.read_text()[:LONG_CHARS]
                   + "\n\nSummarise what this module does in five sentences.")
    rows = []
    for key, (names, cfg) in distinct_models(args.agents).items():
        pair = {}
        for build, (binary, libdir) in builds.items():
            if gpu_used() > args.max_used_mib:
                print(f"refusing: the card holds {gpu_used()} MiB "
                      f"(> {args.max_used_mib}); unload the live model first",
                      file=sys.stderr)
                return 2
            tag = f"{names[0]}-{build}"
            print(f"[{time.strftime('%H:%M:%S')}] {'/'.join(names)} on {build} ...",
                  flush=True)
            res = measure(binary, libdir, cfg, long_prompt, tag, args.n_predict,
                          args.max_used_mib)
            row = {"agents": names, "build": build, **res}
            rows.append(row)
            pair[build] = res
            with args.out.open("a") as f:
                f.write(json.dumps({k: v for k, v in row.items()
                                    if k != "short_output"}) + "\n")
        if all("short_output" in r for r in pair.values()):
            rows[-1]["parity"] = rows[-2]["parity"] = _parity(
                pair["old"]["short_output"], pair["new"]["short_output"])

    print(f"\n{'agents':<40} {'build':<5} {'VRAM':>6} {'free':>6} "
          f"{'pp short':>9} {'tg short':>9} {'pp long':>8} {'tg long':>8}  parity")
    for r in rows:
        if "error" in r:
            print(f"{'/'.join(r['agents']):<40} {r['build']:<5} ERROR {r['error']}")
            continue
        s, lg = r["short"], r["long"]
        print(f"{'/'.join(r['agents']):<40} {r['build']:<5} {r['vram_mib']:>6} "
              f"{r['free_mib']:>6} {s['prefill_tps'] or 0:>9.1f} {s['decode_tps'] or 0:>9.2f} "
              f"{lg['prefill_tps'] or 0:>8.1f} {lg['decode_tps'] or 0:>8.2f}  "
              f"{r.get('parity', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
