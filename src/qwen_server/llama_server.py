"""LlamaServerManager: subprocess lifecycle + HTTP proxy for llama-server.

Extracted from server.py for organization. Owns the single llama-server
child process used by all agents — only one model is loaded at a time;
switching agents triggers a model swap.
"""
import hashlib
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from collections import OrderedDict
from threading import Lock, Thread
from typing import Any, Dict, Iterator, List, Optional

import requests as http_requests
from fastapi import HTTPException

from qwen_server.config import Config


class InsufficientVramError(RuntimeError):
    """Raised when a pre-flight check determines a swap would OOM.

    Caller (server.chat_completions) translates this to a 503 with
    Retry-After so the orchestrator can back off instead of triggering
    the CUDA crash loop we observed 2026-05-04.
    """


# State machine for the llama-server child process. Exposed via snapshot()
# so the dashboard can show "swap in progress" vs "running" rather than
# guessing from process.poll().
_STATE_IDLE = "idle"
_STATE_STARTING = "starting"
_STATE_RUNNING = "running"
_STATE_STOPPING = "stopping"
from qwen_server.streaming import (
    ThinkingStripper, build_completion_response,
    build_stream_chunk, strip_thinking,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Llama-Server Subprocess Manager
# ============================================================================

class LlamaServerManager:
    """Manages the llama-server subprocess that serves all agents."""

    LLAMA_SERVER_PORT = 8081
    # Idle timeout — only counts when no requests are active. The watchdog
    # also checks _active_requests below and never kills mid-stream.
    IDLE_TIMEOUT = 1800  # 30 minutes
    HEALTH_POLL_INTERVAL = 0.5
    HEALTH_TIMEOUT = 120  # seconds to wait for /health
    # Safety margin on top of the recorded footprint when pre-flighting a
    # swap. Reserves headroom for the compute buffer to grow during prefill.
    _VRAM_MARGIN_MIB = 500

    # GGML type enum → llama-server --cache-type string
    _CACHE_TYPE_NAMES = {
        0: 'f32', 1: 'f16', 2: 'q4_0', 3: 'q4_1',
        6: 'q5_0', 7: 'q5_1', 8: 'q8_0', 9: 'q8_1',
    }

    def __init__(self):
        # Two-lock discipline:
        # - self.lock is held only for SHORT critical sections (state attribute
        #   reads/writes). Readers — is_running, has_active_requests, snapshot
        #   — only ever wait on this short lock.
        # - self._swap_lock is held during the SLOW operations of a model
        #   swap or shutdown (subprocess spawn, /health poll loop, SIGTERM
        #   wait). It serializes concurrent ensure_running calls without
        #   blocking the dashboard's snapshot polls. Ordering is always
        #   (acquire _swap_lock first, then briefly self.lock) — no nested
        #   acquisitions in the other order.
        self.lock = Lock()
        self._swap_lock = Lock()
        self._state = _STATE_IDLE
        self.process: Optional[subprocess.Popen] = None
        self.current_model_path: Optional[str] = None
        # Most recent agent_id passed through ensure_running. Multiple agents
        # can share a model path (supervisor + q36_architect both use Qwen3.6-27B),
        # so this is tracked separately from current_model_path — set on every
        # ensure_running call, not just on the swap.
        self.current_agent_id: Optional[str] = None
        self.current_model_config: Optional[dict] = None
        # Tuple of fields that must match for the running child to be reused.
        # Path equality alone isn't enough — fast_implementer (n_ctx=196608,
        # n_ubatch=3584) and debugger (n_ctx=131072, n_ubatch=4096) point at
        # the same Qwen3-Coder-30B GGUF file, so a path-only check let the
        # second caller silently keep the first caller's runtime config.
        self.current_runtime_signature: Optional[tuple] = None
        self.started_at: Optional[float] = None
        # Per-agent VRAM consumption in MiB, learned on the first successful
        # start. Empty until each agent has been loaded once. Used by the
        # pre-flight check before a swap so we can refuse instead of crashing.
        self._measured_vram_delta_mib: Dict[str, int] = {}
        # LRU cache of tokenize results, keyed by md5(content). System
        # prompts + few-shot examples are stable across requests, so we
        # save a /tokenize round-trip on most calls.
        self._tokenize_cache: "OrderedDict[str, int]" = OrderedDict()
        self._tokenize_cache_max = 256
        self.last_request_time: float = 0
        # Number of in-flight requests. Watchdog skips the kill while >0.
        # Race-free under CPython's GIL for ±1 increments.
        self._active_requests: int = 0
        self._watchdog_thread: Optional[Thread] = None
        self._watchdog_running = False

    def start(self, model_config: dict):
        """Spawn llama-server with the given model config, wait for /health.

        Caller responsibility: hold ``self._swap_lock`` for the duration of
        this call. We do NOT hold ``self.lock`` during the multi-second
        subprocess spawn or the up-to-120-second /health poll loop;
        ``self.lock`` is only acquired briefly to publish state transitions
        (idle → starting → running) so dashboard readers don't block.
        """
        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools')
        binary = os.path.join(tools_dir, 'llama-server')

        if not os.path.isfile(binary):
            raise FileNotFoundError(f"llama-server binary not found: {binary}")

        model_path = model_config['path']
        # Map GGML type_k/type_v integers to llama-server flag strings
        cache_k = self._CACHE_TYPE_NAMES.get(model_config.get('type_k', 8), 'q8_0')
        cache_v = self._CACHE_TYPE_NAMES.get(model_config.get('type_v', 8), 'q8_0')

        cmd = [
            binary,
            '-m', model_path,
            '-ngl', str(model_config.get('n_gpu_layers', 0)),
            '-c', str(model_config.get('n_ctx', 32768)),
            '-b', str(model_config.get('n_batch', 2048)),
            '-ub', str(model_config.get('n_ubatch', 512)),
            '-t', str(Config.DEFAULT_N_THREADS),
            '-tb', str(Config.DEFAULT_N_THREADS_BATCH),
            '-fa', 'auto',
            '--mmap',
            '--cache-type-k', cache_k,
            '--cache-type-v', cache_v,
            '--host', '127.0.0.1',
            '--port', str(self.LLAMA_SERVER_PORT),
            '-np', '1',
            '--lookup-cache-dynamic', '/tmp/llama-lookup-cache.bin',
            # On retries (autonomous flow rebuilds the prompt from scratch),
            # llama-server scans the new prompt against the cached KV state
            # and reuses the longest matching prefix. 256 is the minimum
            # match length; larger values miss shorter common prefixes.
            '--cache-reuse', '256',
        ]

        # MoE models: keep expert weights on CPU, put more attention layers on GPU
        if model_config.get('cpu_moe'):
            cmd.append('--cpu-moe')

        # Speculative decoding: pair a smaller same-tokenizer draft model with
        # the target. Draft predicts N tokens, target verifies them in a single
        # forward pass — accepted prefix is free decode. Net win depends on
        # draft tps and acceptance rate; on cpu_moe targets the draft and
        # target compete for memory bandwidth, so measure before trusting.
        draft = model_config.get('draft')
        if draft:
            draft_cache_k = self._CACHE_TYPE_NAMES.get(draft.get('type_k', 8), 'q8_0')
            draft_cache_v = self._CACHE_TYPE_NAMES.get(draft.get('type_v', 8), 'q8_0')
            cmd.extend([
                '-md', draft['path'],
                '-ngld', str(draft.get('n_gpu_layers', 0)),
                '-cd', str(draft.get('n_ctx', model_config.get('n_ctx', 32768))),
                '-ctkd', draft_cache_k,
                '-ctvd', draft_cache_v,
                '--draft-max', str(draft.get('draft_max', 4)),
                '--draft-min', str(draft.get('draft_min', 1)),
                '--draft-p-min', str(draft.get('draft_p_min', 0.75)),
            ])
            # Force draft to skip device offload. With ngld=0 the weights are
            # already CPU-resident, but the graph scheduler inherits CUDA from
            # the target and tries to allocate a multi-GB compute buffer on
            # GPU0 — fails when target leaves <1 GB free. `-devd none` means
            # "don't offload draft to any device" → CPU-only scheduler.
            device_draft = draft.get('device', 'none')
            cmd.extend(['-devd', device_draft])
            if draft.get('cpu_moe'):
                cmd.append('-cmoed')

        # Add model-specific server args (chat template, jinja, etc.)
        extra_args = model_config.get('server_extra_args', ['--chat-template', 'chatml'])
        cmd.extend(extra_args)

        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = tools_dir + ':' + env.get('LD_LIBRARY_PATH', '')

        # Mark "starting" before we Popen so concurrent snapshots see the
        # transition rather than a stale "idle". State writes are batched
        # under self.lock for consistency.
        with self.lock:
            self._state = _STATE_STARTING

        logger.info("Starting llama-server: %s", ' '.join(cmd))
        process = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        with self.lock:
            self.process = process
            self.current_model_path = model_path
            self.current_model_config = model_config
            self.current_runtime_signature = self._runtime_signature(model_config)
            self.started_at = time.time()

        # Background thread to drain stdout so the pipe doesn't block
        def _drain_output(proc):
            try:
                for line in iter(proc.stdout.readline, b''):
                    logger.info("[llama-server] %s", line.decode('utf-8', errors='replace').rstrip())
            except (ValueError, OSError):
                pass  # Process closed
        drain_thread = Thread(target=_drain_output, args=(process,), daemon=True)
        drain_thread.start()

        # Poll /health until ready. This loop runs WITHOUT self.lock so
        # the dashboard's snapshot polls don't hang for the up-to-120s
        # this can take. process.poll() and http_requests.get() are both
        # safe to call concurrently with state reads.
        health_url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/health"
        deadline = time.time() + self.HEALTH_TIMEOUT
        start_t = time.time()
        while time.time() < deadline:
            if process.poll() is not None:
                # Don't leak state on exit-during-startup; clear it so the
                # next ensure_running starts from a clean slate.
                with self.lock:
                    self._state = _STATE_IDLE
                    self.process = None
                    self.current_model_path = None
                    self.current_model_config = None
                    self.current_runtime_signature = None
                    self.started_at = None
                raise RuntimeError(
                    f"llama-server exited with code {process.returncode} during startup"
                )
            try:
                resp = http_requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    logger.info("llama-server healthy after %.1fs", time.time() - start_t)
                    with self.lock:
                        self._state = _STATE_RUNNING
                        self.last_request_time = time.time()
                    self._start_watchdog()
                    return
            except (http_requests.ConnectionError, http_requests.Timeout):
                # Both are normal during startup: refused-connection while
                # the server isn't listening yet, then read-timeout if it
                # accepts but isn't ready.
                pass
            time.sleep(self.HEALTH_POLL_INTERVAL)

        # Timeout — kill the process. _shutdown_unlocked also runs under
        # self._swap_lock (which we hold) and clears state to idle.
        self._shutdown_unlocked()
        raise TimeoutError(f"llama-server did not become healthy within {self.HEALTH_TIMEOUT}s")

    def _shutdown_unlocked(self):
        """Internal: stop the subprocess. Caller must hold self._swap_lock.

        Releases self.lock during the SIGTERM/wait so dashboard snapshots
        don't hang for up to 15s while we're killing the child. Lock-free
        operations only touch the local ``proc`` reference; concurrent
        readers see _state == STOPPING and self.process == None as soon as
        we transition.
        """
        # Capture the process handle and clear "current" state under a
        # short lock acquisition. After this block, readers see "stopping"
        # state with no process attached; the actual SIGTERM/wait runs
        # without self.lock.
        with self.lock:
            proc = self.process
            if proc is None:
                self._state = _STATE_IDLE
                return
            self._watchdog_running = False
            self._state = _STATE_STOPPING
            self.process = None
            self.current_model_path = None
            self.current_model_config = None
            self.current_agent_id = None
            self.current_runtime_signature = None
            self.started_at = None

        pid = proc.pid
        logger.info("Shutting down llama-server (PID %d)...", pid)
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
                logger.info("llama-server (PID %d) terminated gracefully", pid)
            except subprocess.TimeoutExpired:
                logger.warning("llama-server (PID %d) didn't exit, sending SIGKILL", pid)
                proc.kill()
                proc.wait(timeout=5)
        except Exception as e:
            logger.error("Error shutting down llama-server: %s", e)

        with self.lock:
            self._state = _STATE_IDLE

    def _gpu_free_mib(self) -> Optional[int]:
        """Query free VRAM (device 0) via nvidia-smi. Returns None on failure.

        Cheap call (~30 ms). Used by the swap-VRAM-release wait below; we
        deliberately don't hold a long-running NVML handle because the
        watchdog/teardown threads need this to be reentrant-safe and the
        cost is negligible.
        """
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return None
            first_line = result.stdout.strip().split("\n")[0].strip()
            return int(first_line)
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            return None

    def _wait_for_vram_release(self, *, max_wait: float = 8.0, stable_for: float = 0.6) -> None:
        """Block until free VRAM has stabilized after a model teardown.

        CUDA reclaims VRAM asynchronously: ``process.wait()`` returns when
        the OS reaps the zombie, but the kernel may still be finalizing the
        freed allocations for 1-2s after that. If we start the next model
        immediately, its compute-buffer allocation can OOM even though the
        old weights are technically freed — we observed this three times
        on the architect (q36_architect, ~14 GiB resident) → fast_implementer
        (Coder-30B-A3B, 4.7 GiB compute buffer) swap, with the new process
        exiting on cudaMalloc failure 0.5-1s into startup.

        Strategy: poll nvidia-smi until two consecutive readings agree
        within ~50 MiB for at least ``stable_for`` seconds. That's the
        signal that the kernel has finished its async free work. Bounded
        by ``max_wait`` so a stuck driver can't hang the swap forever.
        """
        deadline = time.time() + max_wait
        last_free: Optional[int] = None
        stable_since: Optional[float] = None

        while time.time() < deadline:
            free_mib = self._gpu_free_mib()
            if free_mib is None:
                # nvidia-smi unavailable — fall back to a fixed sleep so the
                # swap still gets *some* breathing room.
                time.sleep(2.0)
                return

            if last_free is not None and abs(free_mib - last_free) <= 50:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_for:
                    logger.info("VRAM stabilized: %d MiB free after teardown", free_mib)
                    return
            else:
                stable_since = None

            last_free = free_mib
            time.sleep(0.2)

        logger.warning(
            "VRAM release timeout after %.1fs (last reading: %s MiB) — proceeding with start",
            max_wait, last_free,
        )

    def shutdown(self):
        """Stop the subprocess gracefully, then force-kill if needed. Thread-safe.

        Acquires ``_swap_lock`` so this serializes against any in-flight
        ensure_running. The shutdown can take up to 15s (SIGTERM wait +
        SIGKILL fallback); ``self.lock`` is held only for short bursts
        inside ``_shutdown_unlocked``.
        """
        with self._swap_lock:
            self._shutdown_unlocked()

    def is_running(self) -> bool:
        """True if the manager is in the RUNNING state and the child is alive.

        Distinct from "process exists" — during STARTING we have a Popen
        handle but llama-server hasn't passed /health yet, so requests
        will fail. Callers that want to know "can I send a request?"
        want this RUNNING check, not just process existence.
        """
        with self.lock:
            return (
                self._state == _STATE_RUNNING
                and self.process is not None
                and self.process.poll() is None
            )

    def has_active_requests(self) -> bool:
        """True if any sync/stream request is mid-flight against llama-server."""
        with self.lock:
            return self._active_requests > 0

    @staticmethod
    def _runtime_signature(mc: dict) -> tuple:
        """Tuple of fields that materially change llama-server behavior.

        Two agents sharing a path can still need different runtimes
        (n_ctx, sampler bias, server flags). We compare on the full
        signature so a swap is forced when any of these differ — even
        if the underlying GGUF file is identical. ``logit_bias`` and
        ``draft`` are JSON-serialized for stable comparison; the
        ordering of these inputs comes from Config so sort_keys is a
        cheap belt-and-braces guard against future churn.
        """
        return (
            mc.get('path'),
            mc.get('n_ctx'),
            mc.get('n_batch'),
            mc.get('n_ubatch'),
            mc.get('n_gpu_layers'),
            mc.get('type_k'),
            mc.get('type_v'),
            bool(mc.get('cpu_moe')),
            json.dumps(mc.get('logit_bias'), sort_keys=True)
            if mc.get('logit_bias') is not None else None,
            tuple(mc.get('server_extra_args') or ()),
            json.dumps(mc.get('draft'), sort_keys=True)
            if mc.get('draft') is not None else None,
        )

    def _check_vram_or_raise(self, agent_id: Optional[str]) -> None:
        """Pre-flight: refuse to start if we've previously measured this
        agent's VRAM footprint and there isn't enough free for it.

        First-time loads bypass the check (we have no measurement yet) —
        this is intentional. Once we've successfully loaded an agent and
        recorded its delta, subsequent loads gate on `free >= delta + margin`.
        """
        if agent_id is None:
            return
        recorded = self._measured_vram_delta_mib.get(agent_id)
        if recorded is None:
            return
        free = self._gpu_free_mib()
        if free is None:
            return  # nvidia-smi unavailable — proceed and hope for the best
        needed = recorded + self._VRAM_MARGIN_MIB
        if free < needed:
            raise InsufficientVramError(
                f"refusing to load {agent_id}: needs ~{recorded} MiB "
                f"(+{self._VRAM_MARGIN_MIB} margin) but only {free} MiB free"
            )

    def ensure_running(self, model_config: dict, agent_id: Optional[str] = None):
        """Ensure llama-server is running with the correct model. Handles model swaps.

        ``agent_id`` is recorded so the dashboard can show which logical agent
        is currently bound to the loaded model. Multiple agent_ids share a
        single model path, so this is updated on every call, not only on swap.

        Swap detection compares on the full runtime signature, not just on
        ``path`` — see ``_runtime_signature`` for why path-equality wasn't
        enough. Before every (re)start, ``_check_vram_or_raise`` consults
        the per-agent measured footprint and raises ``InsufficientVramError``
        rather than letting the child crash on a cudaMalloc failure.
        """
        signature = self._runtime_signature(model_config)
        # _swap_lock is held for the entire transition (potentially many
        # seconds). self.lock is acquired in short bursts inside this
        # block — readers (snapshot, is_running) see consistent state and
        # don't have to wait for the slow operations.
        with self._swap_lock:
            with self.lock:
                already_correct = (
                    self._state == _STATE_RUNNING
                    and self.current_runtime_signature == signature
                )
                if already_correct:
                    self.last_request_time = time.time()
                    if agent_id is not None:
                        self.current_agent_id = agent_id
                    return
                need_swap = self._state == _STATE_RUNNING
                same_path = (
                    need_swap
                    and self.current_model_path == model_config.get('path')
                )

            if need_swap:
                # Runtime drift — even if the GGUF file is the same, n_ctx
                # / sampler bias / server flags can differ between agents
                # that share a path. Shut down and reload to honor the new
                # config. _wait_for_vram_release handles the kernel's async
                # free.
                if same_path:
                    logger.info(
                        "Runtime swap (same model file, different config) for %s",
                        agent_id or '?',
                    )
                else:
                    logger.info("Model swap: shutting down llama-server for new model")
                self._shutdown_unlocked()
                self._wait_for_vram_release()

            # Pre-flight VRAM check AFTER teardown + release wait, so we
            # only refuse when the actual freed-state still won't fit.
            self._check_vram_or_raise(agent_id)
            free_before = self._gpu_free_mib()

            try:
                self.start(model_config)
                if agent_id is not None:
                    with self.lock:
                        self.current_agent_id = agent_id
                    free_after = self._gpu_free_mib()
                    if free_before is not None and free_after is not None:
                        delta = free_before - free_after
                        if delta > 0:
                            with self.lock:
                                self._measured_vram_delta_mib[agent_id] = delta
                            logger.info(
                                "[vram] %s footprint: %d MiB (free %d -> %d)",
                                agent_id, delta, free_before, free_after,
                            )
            except Exception as e:
                logger.error("Failed to start llama-server for %s: %s",
                             model_config.get('path'), e)
                # start() / _shutdown_unlocked already cleared state on
                # their failure paths; re-raise so the caller gets the
                # original exception type for proper error categorization.
                raise

    def snapshot(self) -> dict:
        """Return a JSON-safe view of the current llama-server state.

        Used by the dashboard's ActiveModelCard. Captures the configuration
        knobs that meaningfully change inference behavior (KV quant, ngl,
        ub, cpu_moe, draft); skips internals (env, server_extra_args) since
        they're not actionable at the dashboard level.
        """
        # Pull all state under one short lock acquisition so the dict we
        # build outside the lock is internally consistent. Snapshot() is
        # called at 0.5 Hz from the dashboard — we deliberately don't
        # acquire _swap_lock here, so reads stay fast even during a
        # multi-second model swap.
        with self.lock:
            state = self._state
            proc = self.process
            agent_id = self.current_agent_id
            model_path = self.current_model_path
            model_config = self.current_model_config
            started_at = self.started_at
            last_request_time = self.last_request_time
            active_requests = self._active_requests
            pid = proc.pid if proc is not None else None
            proc_alive = proc is not None and proc.poll() is None

        if not proc_alive:
            return {
                'running': False,
                'state': state,
                'agent_id': agent_id,
                'model_path': model_path,
                'model_basename': os.path.basename(model_path) if model_path else None,
                'pid': pid,
                'idle_timeout_s': self.IDLE_TIMEOUT,
                'active_requests': active_requests,
                'last_request_seconds_ago': None,
                'uptime_seconds': None,
                'config': None,
            }

        cfg = model_config or {}
        draft_summary = None
        draft = cfg.get('draft')
        if draft:
            draft_summary = {
                'path': draft.get('path'),
                'basename': os.path.basename(draft.get('path', '')) or None,
                'n_gpu_layers': draft.get('n_gpu_layers'),
                'n_ctx': draft.get('n_ctx'),
                'cpu_moe': draft.get('cpu_moe', False),
            }

        now = time.time()
        return {
            'running': state == _STATE_RUNNING,
            'state': state,
            'agent_id': agent_id,
            'model_path': model_path,
            'model_basename': os.path.basename(model_path) if model_path else None,
            'pid': pid,
            'idle_timeout_s': self.IDLE_TIMEOUT,
            'active_requests': active_requests,
            'last_request_seconds_ago': (
                int(now - last_request_time) if last_request_time else None
            ),
            'uptime_seconds': int(now - started_at) if started_at else None,
            'config': {
                'n_ctx': cfg.get('n_ctx'),
                'n_batch': cfg.get('n_batch'),
                'n_ubatch': cfg.get('n_ubatch'),
                'n_gpu_layers': cfg.get('n_gpu_layers'),
                'cpu_moe': cfg.get('cpu_moe', False),
                'cache_type_k': self._CACHE_TYPE_NAMES.get(cfg.get('type_k', 8), 'q8_0'),
                'cache_type_v': self._CACHE_TYPE_NAMES.get(cfg.get('type_v', 8), 'q8_0'),
                'repeat_penalty': cfg.get('repeat_penalty'),
                'repeat_last_n': cfg.get('repeat_last_n'),
                'draft': draft_summary,
            },
        }

    def _start_watchdog(self):
        """Start the idle watchdog thread."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_running = True
        self._watchdog_thread = Thread(target=self._idle_watchdog, daemon=True)
        self._watchdog_thread.start()

    def _idle_watchdog(self):
        """Background thread: shut down subprocess if idle for IDLE_TIMEOUT seconds.

        Skips kill while any request is in-flight. Without this guard the
        watchdog would race with long synchronous requests (architect /
        reviewer generations that exceed IDLE_TIMEOUT) and SIGKILL the
        subprocess mid-response, dropping the client's connection.

        Acquires self._swap_lock non-blocking before tearing down — if a
        swap is in progress, that thread is already managing teardown and
        we just exit. Holding the lock blocking would risk an awkward
        cross-thread serialization where the watchdog sits behind a slow
        ensure_running call only to find there's nothing to kill.
        """
        while self._watchdog_running:
            time.sleep(30)  # Check every 30s
            if not self._watchdog_running:
                break
            with self.lock:
                if self._state != _STATE_RUNNING or self.process is None:
                    break
                if self._active_requests > 0:
                    # In-flight request — don't shut down. Treat as activity.
                    self.last_request_time = time.time()
                    continue
                idle = time.time() - self.last_request_time
                should_kill = idle >= self.IDLE_TIMEOUT
            if should_kill:
                logger.info(
                    "llama-server idle for %.0fs, shutting down to free resources",
                    idle,
                )
                if self._swap_lock.acquire(blocking=False):
                    try:
                        self._shutdown_unlocked()
                    finally:
                        self._swap_lock.release()
                else:
                    logger.info(
                        "Swap in progress — watchdog deferring to ensure_running"
                    )
                break

    def tokenize(self, text: str) -> int:
        """Return the token count for ``text`` via llama-server's /tokenize.

        Cached LRU-style by content hash so the system prompt + few-shot
        examples (stable across requests) only round-trip once. Caller
        must handle exceptions — this method propagates network errors,
        404s on cold-start, etc., so the caller can fall back to a
        char-based estimate without us silently masking real failures.
        """
        if not text:
            return 0
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        cached = self._tokenize_cache.get(key)
        if cached is not None:
            # Move-to-end for LRU semantics.
            self._tokenize_cache.move_to_end(key)
            return cached

        url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/tokenize"
        # Short timeout — /tokenize is CPU-bound and quick. If it stalls
        # this long, llama-server is unhealthy and the caller should
        # fall back rather than blocking the request.
        resp = http_requests.post(url, json={"content": text}, timeout=5)
        resp.raise_for_status()
        count = len(resp.json().get("tokens", []))

        self._tokenize_cache[key] = count
        if len(self._tokenize_cache) > self._tokenize_cache_max:
            # Evict the LRU entry.
            self._tokenize_cache.popitem(last=False)
        return count

    def _post_with_recovery(self, url: str, payload: dict, *, stream: bool,
                            model_config: Optional[dict], rid: str):
        """POST to llama-server with one-shot crash recovery.

        If the request fails with a ConnectionError AND the child process
        has died (poll() != None), restart it and retry the request once.
        Connection errors with a healthy child mean network glitch and
        propagate immediately; restart loops are bounded at 1 retry to
        avoid hammering an unhealthy model with requests it can't serve.

        Caller is responsible for closing/iterating the returned response
        and for the active_requests bookkeeping.
        """
        last_exc = None
        for attempt in range(2):
            try:
                return http_requests.post(
                    url, json=payload, stream=stream,
                    timeout=Config.LLAMA_SERVER_REQUEST_TIMEOUT,
                )
            except http_requests.ConnectionError as e:
                last_exc = e
                with self.lock:
                    child_dead = (
                        self.process is None or self.process.poll() is not None
                    )
                    rc = (
                        self.process.returncode if self.process is not None else None
                    )
                if attempt == 0 and child_dead and model_config is not None:
                    logger.warning(
                        "[%s] llama-server child died (rc=%s) mid-request — "
                        "restarting and retrying once", rid, rc,
                    )
                    try:
                        # ensure_running internally observes the dead process
                        # via poll() and starts a fresh one. agent_id stays
                        # whatever it was before the crash so dashboards
                        # don't see a transient blank.
                        self.ensure_running(
                            model_config, agent_id=self.current_agent_id,
                        )
                        continue
                    except Exception as restart_exc:
                        logger.error(
                            "[%s] restart after crash failed: %s",
                            rid, restart_exc,
                        )
                        raise restart_exc from e
                # Either healthy child + connection glitch, or already
                # retried — give up.
                raise
        # Unreachable, but keep type-checkers and explicit-return purists happy.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable")

    @staticmethod
    def _build_request_payload(openai_messages: List[dict], max_tokens: int,
                               temperature: float, stream: bool,
                               model_config: Optional[dict],
                               tools: Optional[List[Dict[str, Any]]],
                               tool_choice: Optional[Any],
                               parallel_tool_calls: Optional[bool]) -> dict:
        mc = model_config or {}
        payload = {
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
            # No explicit stop sequences — llama-server's chat template handles
            # end-of-turn tokens (im_end, EOT, etc.) natively. Passing them here
            # causes premature stopping via double-matching.
            "repeat_penalty": mc.get('repeat_penalty', 1.15),
            "repeat_last_n": mc.get('repeat_last_n', 256),
            # Ban model-specific native tool tokens to prevent format corruption.
            "logit_bias": mc.get('logit_bias', []),
        }
        if tools:
            payload["tools"] = tools
            # The chatml/logit_bias workaround for marker-only models bans the
            # very tokens (<tool_call>, etc.) that native tool-calling needs to
            # emit. Drop the bias when the caller opts into native tools.
            payload["logit_bias"] = []
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        return payload

    def proxy_stream(self, messages: List[dict], system_prompt: str,
                     model_id: str, max_tokens: int, temperature: float,
                     model_config: dict = None,
                     est_prompt_tokens: int = 0,
                     tools: Optional[List[Dict[str, Any]]] = None,
                     tool_choice: Optional[Any] = None,
                     parallel_tool_calls: Optional[bool] = None,
                     req_id: Optional[str] = None) -> Iterator[str]:
        """Stream a chat completion via the llama-server subprocess.

        ``req_id`` is the correlation ID assigned by the metrics middleware;
        we prefix it onto every log line so a grep on `req_xxxxxxxx` shows
        the full lifecycle of one request from entry through proxy to
        upstream errors.
        """
        rid = req_id or "req_unknown"
        with self.lock:
            self.last_request_time = time.time()
            self._active_requests += 1
        try:
            # Emit progress event so client can display prompt size during prefill
            n_ctx = (model_config or {}).get('n_ctx', 32768)
            progress_event = {"type": "progress", "stage": "prefill",
                              "prompt_tokens": est_prompt_tokens, "n_ctx": n_ctx}
            yield f"data: {json.dumps(progress_event)}\n\n"

            openai_messages = self._build_openai_messages(messages, system_prompt)
            url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/v1/chat/completions"
            payload = self._build_request_payload(
                openai_messages, max_tokens, temperature, True,
                model_config, tools, tool_choice, parallel_tool_calls,
            )

            completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            finish_reason = None
            accumulated_text = []  # Diagnostic: capture full response
            think_stripper = ThinkingStripper()

            try:
                # _post_with_recovery handles the case where the child died
                # before we even connected. Mid-stream deaths still fall
                # through to the broader except below — by then the client
                # has consumed partial chunks and "retry transparently" is
                # a lie we can't deliver.
                resp = self._post_with_recovery(
                    url, payload, stream=True,
                    model_config=model_config, rid=rid,
                )
                with resp:
                    if resp.status_code != 200:
                        # Log full error server-side; return a generic message
                        # to the client. The raw error body can leak model paths,
                        # llama-server version, library internals, and OS errors.
                        logger.error(
                            "[%s] llama-server returned %d: %s",
                            rid, resp.status_code, resp.text,
                        )
                        error_chunk = {"error": {
                            "message": f"upstream inference error (request_id={rid})",
                            "type": "server_error",
                        }}
                        yield f"data: {json.dumps(error_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    resp.encoding = 'utf-8'  # llama-server sends UTF-8; override requests' ISO-8859-1 default
                    for line in resp.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            content = delta.get("content")
                            fr = choices[0].get("finish_reason")
                            if fr:
                                finish_reason = fr

                            # Proxy native tool_calls deltas straight through. Each
                            # delta carries a partial slice (function name, then
                            # incremental argument JSON) keyed by index so the
                            # client reassembles parallel calls.
                            tc = delta.get("tool_calls")
                            if tc:
                                self.last_request_time = time.time()
                                out_chunk = build_stream_chunk(completion_id, model_id, tool_calls=tc)
                                yield f"data: {json.dumps(out_chunk)}\n\n"

                            if content:
                                accumulated_text.append(content)
                                # Strip <think>...</think> blocks from streaming output
                                filtered = think_stripper.feed(content)
                                if filtered:
                                    self.last_request_time = time.time()  # Keep watchdog at bay during long streams
                                    out_chunk = build_stream_chunk(completion_id, model_id, content=filtered)
                                    yield f"data: {json.dumps(out_chunk)}\n\n"
                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                logger.error("[%s] Error proxying stream from llama-server: %s",
                             rid, e, exc_info=True)
                error_chunk = {"error": {"message": str(e), "type": "server_error"}}
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Flush any remaining buffered content from the thinking stripper
            remaining = think_stripper.flush()
            if remaining:
                out_chunk = build_stream_chunk(completion_id, model_id, content=remaining)
                yield f"data: {json.dumps(out_chunk)}\n\n"

            # Log the full response for diagnostics (repr escapes newlines for single-line journald)
            full_text = ''.join(accumulated_text)
            logger.info("[%s] llama-server proxy response (%d chars): %s",
                         rid, len(full_text), repr(full_text[:2000]))

            final_chunk = build_stream_chunk(completion_id, model_id, finish=True,
                                             finish_reason=finish_reason or "stop")
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            with self.lock:
                self.last_request_time = time.time()
                self._active_requests -= 1

    def proxy_sync(self, messages: List[dict], system_prompt: str,
                   model_id: str, max_tokens: int, temperature: float,
                   model_config: dict = None,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   tool_choice: Optional[Any] = None,
                   parallel_tool_calls: Optional[bool] = None,
                   req_id: Optional[str] = None) -> dict:
        """Synchronous chat completion via the llama-server subprocess."""
        rid = req_id or "req_unknown"
        with self.lock:
            self.last_request_time = time.time()

        openai_messages = self._build_openai_messages(messages, system_prompt)
        url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/v1/chat/completions"
        payload = self._build_request_payload(
            openai_messages, max_tokens, temperature, False,
            model_config, tools, tool_choice, parallel_tool_calls,
        )

        with self.lock:
            self._active_requests += 1
        try:
            resp = self._post_with_recovery(
                url, payload, stream=False,
                model_config=model_config, rid=rid,
            )
            if resp.status_code != 200:
                logger.error(
                    "[%s] llama-server returned %d: %s",
                    rid, resp.status_code, resp.text,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"upstream inference error (request_id={rid})",
                )

            result = resp.json()
            message = result["choices"][0].get("message", {})
            text = message.get("content") or ""
            text = strip_thinking(text)
            tool_calls = message.get("tool_calls")
            finish_reason = result["choices"][0].get("finish_reason", "stop")
            usage = result.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

            with self.lock:
                self.last_request_time = time.time()
            return build_completion_response(model_id, text, usage,
                                             finish_reason=finish_reason,
                                             tool_calls=tool_calls)
        finally:
            with self.lock:
                self._active_requests -= 1

    @staticmethod
    def _build_openai_messages(messages: List, system_prompt: str) -> List[dict]:
        """Build OpenAI-format messages array from ChatMessage list + system prompt.

        If the caller already supplied a system message (autonomous mode sends
        task-specific prompts with output-format markers the pipeline parses),
        trust it and skip the server's agent-config system prompt. Injecting
        both produces two system messages, which strict Jinja templates reject
        with 'System message must be at the beginning', and also confuses the
        model when the two prompts conflict.
        """
        def _role(m):
            return m["role"] if isinstance(m, dict) else m.role

        client_has_system = bool(messages) and _role(messages[0]) == "system"

        openai_msgs = []
        if system_prompt and not client_has_system:
            openai_msgs.append({"role": "system", "content": system_prompt})

        def _get(m, key, default=None):
            if isinstance(m, dict):
                return m.get(key, default)
            return getattr(m, key, default)

        for msg in messages:
            role = _get(msg, "role")
            content = _get(msg, "content")
            built = {"role": role, "content": content if content is not None else ""}
            # Preserve OpenAI tool-calling fields when present.
            tool_calls = _get(msg, "tool_calls")
            if tool_calls:
                built["tool_calls"] = tool_calls
                # Assistant turns that carry tool_calls may have null content;
                # llama-server tolerates content="" but some templates require null.
                if not content:
                    built["content"] = None
            tool_call_id = _get(msg, "tool_call_id")
            if tool_call_id:
                built["tool_call_id"] = tool_call_id
            name = _get(msg, "name")
            if name:
                built["name"] = name
            openai_msgs.append(built)
        return openai_msgs

