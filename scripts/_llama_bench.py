#!/usr/bin/env python3
"""Shared harness for the standalone llama-server benchmarks.

These scripts spawn their OWN llama-server on a scratch port to measure raw
flag combinations. That is a different job from benchmark_decode.py /
benchmark_prefill.py, which drive the live FastAPI server through its agents.

Freeing the GPU means killing the running server's llama-server child. The
FastAPI server stays up and LlamaServerManager respawns the child on the next
real request, so this is safe to run against a live but idle server.

Paths derive from this file's location. The previous copies of these scripts
lived loose in ~/Dev and hardcoded ~/Dev/qwen-server/tools, so the repo rename
silently left them pointing at a directory that no longer exists.

Run with the venv python — sweep_cpu_moe.py reads the real agent configs:
    ./venv/bin/python scripts/sweep_cpu_moe.py
"""
import os
import signal
import subprocess
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
BINARY = os.path.join(TOOLS, "llama-server")

# Scratch port. The live server's child owns 8081; keep off it so a request
# that lands mid-sweep respawns the real child without colliding with ours.
PORT = 5099

# One prompt across every script so numbers stay comparable run to run.
PROMPT = (
    "Write a detailed technical explanation of how a capability-tiering streaming "
    "architecture works, where a certified TV shell measures device capabilities and a "
    "server evaluates them per-feature against declarative policy to derive a cosmetic "
    "tier label as an output, never an input. Cover the two-round probe-plan/resolve "
    "handshake, the recursive predicate evaluator that records the deepest failing leaf, "
    "graceful-degradation rungs, and why an LG C9 with great decode hardware but an old "
    "JS engine is denied premium multi-angle for exactly one recorded reason. Then give "
    "a precise TypeScript signature for the resolver function and its return type."
)


def gpu_used() -> int:
    """VRAM in use, MiB."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    ).decode().strip().splitlines()[0]
    return int(out)


def gpu_total() -> int:
    """Total VRAM, MiB. Measured, not hardcoded — the old scripts pinned 16303."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
    ).decode().strip().splitlines()[0]
    return int(out)


def wait_gpu_free(limit: int = 2000, tries: int = 40) -> bool:
    """Poll until VRAM drops below `limit`. Blackwell can take 10-30s to release."""
    for _ in range(tries):
        if gpu_used() < limit:
            return True
        time.sleep(2)
    return False


def free_gpu() -> None:
    """Kill the live server's llama-server child so the card is ours.

    The pattern is derived from TOOLS, not hardcoded — that is exactly what
    broke when qwen-server became coding-model-server.
    """
    subprocess.run(["pkill", "-f", os.path.join(TOOLS, "llama-server")], check=False)
    if not wait_gpu_free():
        print(f"warning: GPU still holding {gpu_used()} MiB after pkill", flush=True)


def restore_server() -> None:
    """Restart coding-model-server so it comes back with clean manager state.

    free_gpu() kills the child out from under the live manager. Two things then
    bite on the next real request, and a restart clears both:

      - the manager's per-agent VRAM footprint dict is in-memory, and a recorded
        implementer footprint leaves less headroom than _VRAM_MARGIN_MIB, so a
        *reload* is refused with 503 even though the first load fits;
      - any state the child left behind.

    Sudo-free via the polkit rule in /etc/polkit-1/rules.d/49-coding-model.rules.
    """
    print("restoring coding-model-server (clean manager state)...", flush=True)
    r = subprocess.run(["systemctl", "restart", "coding-model-server.service"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"warning: could not restart the service: {r.stderr.strip()}\n"
              f"         run: systemctl restart coding-model-server.service", flush=True)


def measure(flags: list[str], *, binary: str = BINARY, libdir: str = TOOLS,
            prompt: str = PROMPT, npred: int = 256,
            log_tag: str = "bench") -> dict:
    """Spawn llama-server with `flags`, run one fixed prompt, tear it down.

    Returns llama-server's own timings plus resident VRAM, or {"error": ...}
    if it failed to load (an OOM at low --n-cpu-moe shows up here as a
    non-zero exit before /health ever answers).

    LD_LIBRARY_PATH is required: the binary loads libllama-server-impl.so from
    its own directory and will not start without it.
    """
    env = dict(os.environ, LD_LIBRARY_PATH=libdir)
    log_path = f"/tmp/llama-bench-{log_tag}.log"
    log = open(log_path, "w")
    proc = subprocess.Popen([binary] + flags, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        healthy = False
        for _ in range(300):
            if proc.poll() is not None:
                return {"error": f"server exited (code {proc.returncode}); see {log_path}"}
            try:
                if requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                    healthy = True
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        if not healthy:
            return {"error": f"health timeout; see {log_path}"}

        # Warm the graph. cache_prompt=False so the measured call is a fresh prefill.
        requests.post(f"http://127.0.0.1:{PORT}/completion",
                      json={"prompt": "Hello.", "n_predict": 4, "temperature": 0,
                            "cache_prompt": False}, timeout=180)
        vram = gpu_used()  # model + KV fully resident

        r = requests.post(f"http://127.0.0.1:{PORT}/completion",
                          json={"prompt": prompt, "n_predict": npred, "temperature": 0,
                                "seed": 42, "cache_prompt": False}, timeout=400).json()
        t = r.get("timings", {})
        return {
            "vram_mib": vram,
            "prefill_tps": t.get("prompt_per_second"),
            "decode_tps": t.get("predicted_per_second"),
            "prompt_n": t.get("prompt_n"),
            "predicted_n": t.get("predicted_n"),
            "sample": " ".join(r.get("content", "").split())[:180],
        }
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        wait_gpu_free()


def set_flag(flags: list[str], name: str, value: str) -> list[str]:
    """Replace `name`'s value in an argv, appending the pair if absent."""
    out = list(flags)
    if name in out:
        out[out.index(name) + 1] = value
        return out
    return out + [name, value]


def strip_moe_flags(flags: list[str]) -> list[str]:
    """Drop --cpu-moe / --n-cpu-moe N so a sweep can substitute its own."""
    out, i = [], 0
    while i < len(flags):
        if flags[i] == "--cpu-moe":
            i += 1
        elif flags[i] in ("--n-cpu-moe", "-ncmoe"):
            i += 2
        else:
            out.append(flags[i])
            i += 1
    return out
