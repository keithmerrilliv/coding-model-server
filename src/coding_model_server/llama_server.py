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
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, Iterator, List, Optional

import requests as http_requests
from requests.adapters import HTTPAdapter
from fastapi import HTTPException

from coding_model_server.config import Config


class InsufficientVramError(RuntimeError):
    """Raised when a pre-flight check determines a swap would OOM.

    Caller (server.chat_completions) translates this to a 503 with
    Retry-After so the orchestrator can back off instead of triggering
    the CUDA crash loop we observed 2026-05-04.
    """


class ModelBusyError(RuntimeError):
    """Raised when a model swap is needed but a live child is mid-request.

    A swap SIGTERMs the shared llama-server child; doing that while another
    agent's stream is in flight drops that response mid-token. The idle
    watchdog already refuses to kill while ``_active_requests > 0`` — this
    mirrors that guard on the swap path. Caller (routes.chat) translates it
    to a 503 + Retry-After, exactly as it does for InsufficientVramError, so
    the orchestrator backs off and retries once the in-flight request drains.
    """


class UpstreamCancel:
    """Cross-thread handle for aborting an in-flight upstream stream (DEV-158).

    proxy_stream runs in a worker thread and blocks in a socket read between
    SSE lines; when the CLIENT disconnects, Starlette cancels the awaiting
    task but the worker stays blocked until the next token arrives — during
    a long prefill that means the GPU completes minutes of work for a dead
    client while _active_requests pins other agents out with 503s. The
    route's disconnect watcher calls close(), which closes the underlying
    requests.Response and makes the blocked read raise immediately.

    close() is idempotent and safe in either order relative to register():
    a close that lands before the response exists closes it on arrival.
    """

    def __init__(self):
        self._lock = Lock()
        self._resp = None
        self._closed = False

    def register(self, resp) -> None:
        with self._lock:
            self._resp = resp
            if self._closed:
                self._close_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_locked()

    def _close_locked(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
            self._resp = None


# State machine for the llama-server child process. Exposed via snapshot()
# so the dashboard can show "swap in progress" vs "running" rather than
# guessing from process.poll().
_STATE_IDLE = "idle"
_STATE_STARTING = "starting"
_STATE_RUNNING = "running"
_STATE_STOPPING = "stopping"
from coding_model_server.streaming import (
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
    # Host-RAM pool for parking evicted-slot KV (--cache-ram). llama-server's
    # default is 8192 MiB — useless for one 195k-token context, let alone
    # several, on a 188GB box. Bounded (not -1) because the big MoE models'
    # CPU-resident expert weights already claim >100GB and KV is malloc'd,
    # not reclaimable page cache (DEV-408).
    CACHE_RAM_MIB = int(os.getenv('LLAMA_CACHE_RAM_MIB', '32768'))
    # Slot KV persistence across model swaps (--slot-save-path + the
    # /slots/{id}?action=save|restore API). A saved 195k-token prefill
    # restores in seconds instead of ~8 minutes of recompute (DEV-393's
    # measured worst case).
    SLOT_SAVE_ENABLED = os.getenv('LLAMA_SLOT_SAVE', '1') == '1'
    # Below this many cached tokens the file write costs more than the
    # prefill it would save.
    SLOT_SAVE_MIN_TOKENS = int(os.getenv('LLAMA_SLOT_SAVE_MIN_TOKENS', '8192'))
    # LRU cap on the save directory — KV files for big contexts are
    # tens of GB each.
    SLOT_SAVE_MAX_TOTAL_GIB = float(os.getenv('LLAMA_SLOT_SAVE_MAX_TOTAL_GIB', '100'))
    # Save/restore stream tens of GB to/from NVMe; generous but bounded.
    SLOT_IO_TIMEOUT = 180
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

    # Written on every spawn so a llama-server orphaned by a hard daemon
    # death (SIGKILL, OOM-killer) can be identified and reaped on the next
    # start instead of squatting on the port with 14 GiB of VRAM (DEV-157).
    _PIDFILE = Path.home() / ".cache" / "coding-model-server" / "llama-server.pid"

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
        # can share a model path (supervisor + dense_architect both use Qwen3.6-27B),
        # so this is tracked separately from current_model_path — set on every
        # ensure_running call, not just on the swap.
        self.current_agent_id: Optional[str] = None
        self.current_model_config: Optional[dict] = None
        # Whether the running child's chat template opens a <think> block, which
        # decides if proxy_stream must withhold tokens. Read from /props at load.
        # True until proven otherwise — see _probe_expects_thinking.
        self.current_expects_thinking: bool = True
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
        # Consecutive InsufficientVramError refusals per agent. The recorded
        # delta is measured from two nvidia-smi readings tens of seconds
        # apart on a desktop GPU (Xorg/browser also allocate), so it can be
        # inflated past any achievable free value — and it only refreshes on
        # a SUCCESSFUL load, which the refusal itself prevents: a permanent
        # brick with no correction path (DEV-156). After 3 straight refusals
        # the record is treated as suspect and cleared so the next load
        # attempt can re-measure.
        self._vram_refusals: Dict[str, int] = {}
        self._gpu_total_mib_cache: Optional[int] = None
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
        # Generation token, not a boolean. Each _start_watchdog bumps it and
        # binds the new thread to the new value; each shutdown bumps it to
        # invalidate whoever is running. A shared "running" flag had a lost
        #-wakeup race: a fast swap could start the new child while the old
        # watchdog was still asleep, _start_watchdog early-returned because
        # that thread was technically alive, and the old thread then woke,
        # saw the flag its own shutdown had cleared, and exited — leaving
        # the new child with no watchdog and ~14 GiB pinned (DEV-118).
        self._watchdog_generation = 0
        # One shared HTTP session for every call to the llama-server child
        # (proxy, tokenize, health poll). Without it `requests` opened and tore
        # down a TCP connection per request on the hot proxy path. The pool is
        # sized to the chat admission cap so concurrent streams each keep a
        # warm keep-alive connection instead of contending for one.
        self._session = http_requests.Session()
        # Read the admission cap directly rather than importing runtime (which
        # imports this module — a cycle). Kept in sync with runtime.CHAT_MAX_INFLIGHT.
        _chat_max = int(os.getenv("CODING_MODEL_CHAT_MAX_INFLIGHT", "5"))
        _pool = max(_chat_max + 2, 8)
        _adapter = HTTPAdapter(pool_connections=_pool, pool_maxsize=_pool)
        self._session.mount("http://", _adapter)
        self._session.mount("https://", _adapter)

    def _build_server_args(self, binary: str, model_config: dict) -> list[str]:
        """Build the llama-server argv from a model config.

        Pure function of (binary, model_config) — no subprocess side effects —
        so it's unit-testable in isolation. Covers base flags, the optional
        ``cpu_moe`` toggle, the optional speculative-decode ``draft`` block,
        and per-model ``server_extra_args`` (chat template, jinja, etc.).
        """
        # Map GGML type_k/type_v integers to llama-server flag strings
        cache_k = self._CACHE_TYPE_NAMES.get(model_config.get('type_k', 8), 'q8_0')
        cache_v = self._CACHE_TYPE_NAMES.get(model_config.get('type_v', 8), 'q8_0')

        cmd = [
            binary,
            '-m', model_config['path'],
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
            '--cache-ram', str(self.CACHE_RAM_MIB),
        ]

        if self.SLOT_SAVE_ENABLED:
            save_dir = self._slot_save_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            cmd.extend(['--slot-save-path', str(save_dir)])

        # MoE models: keep expert weights off the GPU. `n_cpu_moe` (preferred)
        # keeps only the first N layers' experts on CPU and runs the rest on the
        # GPU (--n-cpu-moe), which is much faster for decode than --cpu-moe (all
        # experts on CPU) when VRAM allows. Falls back to --cpu-moe for configs
        # that don't set n_cpu_moe, so untouched models are unaffected.
        n_cpu_moe = model_config.get('n_cpu_moe')
        if n_cpu_moe is not None:
            cmd.extend(['--n-cpu-moe', str(n_cpu_moe)])
        elif model_config.get('cpu_moe'):
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

        return cmd

    def start(self, model_config: dict):
        """Spawn llama-server with the given model config, wait for /health.

        Caller responsibility: hold ``self._swap_lock`` for the duration of
        this call. We do NOT hold ``self.lock`` during the multi-second
        subprocess spawn or the up-to-120-second /health poll loop;
        ``self.lock`` is only acquired briefly to publish state transitions
        (idle → starting → running) so dashboard readers don't block.
        """
        # tools/ lives at the repo root; this file is at src/coding_model_server/.
        tools_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'tools',
        )
        binary = os.path.join(tools_dir, 'llama-server')

        if not os.path.isfile(binary):
            raise FileNotFoundError(f"llama-server binary not found: {binary}")

        cmd = self._build_server_args(binary, model_config)

        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = tools_dir + ':' + env.get('LD_LIBRARY_PATH', '')

        # Mark "starting" before we Popen so concurrent snapshots see the
        # transition rather than a stale "idle". State writes are batched
        # under self.lock for consistency.
        with self.lock:
            self._state = _STATE_STARTING

        # Reap any orphan holding the port BEFORE spawning: a child of a
        # SIGKILLed daemon keeps serving the previous model and answering
        # /health, so the new child dies on bind while the health poll below
        # happily marks RUNNING against the stale server (DEV-157).
        self._reap_orphan_llama_server()

        logger.info("Starting llama-server: %s", ' '.join(cmd))
        # start_new_session: the child (and anything it spawns) lives in its
        # own process group, so shutdown can kill the whole group and a hard
        # daemon death leaves a group we can identify and reap by pid-file.
        process = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            self._PIDFILE.parent.mkdir(parents=True, exist_ok=True)
            self._PIDFILE.write_text(f"{process.pid}\n")
        except OSError as e:
            logger.warning("could not write llama-server pid-file: %s", e)
        with self.lock:
            self.process = process
            self.current_model_path = model_config['path']
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
                    self.current_expects_thinking = True  # back to the safe default
                    self.started_at = None
                raise RuntimeError(
                    f"llama-server exited with code {process.returncode} during startup"
                )
            try:
                resp = self._session.get(health_url, timeout=2)
                if resp.status_code == 200:
                    # Trust the 200 only while OUR child is alive: a stale
                    # orphan can answer /health after our child died on bind,
                    # which used to mark RUNNING with a dead Popen while the
                    # stale process served the previous model (DEV-157).
                    if process.poll() is not None:
                        with self.lock:
                            self._state = _STATE_IDLE
                            self.process = None
                            self.current_model_path = None
                            self.current_model_config = None
                            self.current_runtime_signature = None
                            self.current_expects_thinking = True
                            self.started_at = None
                        raise RuntimeError(
                            f"llama-server exited with code "
                            f"{process.returncode} but port "
                            f"{self.LLAMA_SERVER_PORT} still answers /health "
                            f"— a stale server is squatting on the port"
                        )
                    logger.info("llama-server healthy after %.1fs", time.time() - start_t)
                    expects_thinking = self._probe_expects_thinking()
                    with self.lock:
                        self._state = _STATE_RUNNING
                        self.current_expects_thinking = expects_thinking
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
        # self._swap_lock (which we hold) and clears state to idle. No slot
        # save: the child never became healthy, so there is no KV worth
        # parking and the HTTP call would hang against a wedged server.
        self._shutdown_unlocked(save_slot=False)
        raise TimeoutError(f"llama-server did not become healthy within {self.HEALTH_TIMEOUT}s")

    def _reap_orphan_llama_server(self) -> None:
        """Kill a llama-server left over from a previous daemon (DEV-157).

        The pid-file identifies OUR orphan; /proc/<pid>/cmdline is verified
        before killing so a recycled pid never takes down an innocent
        process. If the port is still served afterwards by something we
        can't identify, refuse to start — a foreign server answering our
        health checks is strictly worse than failing loudly.
        """
        pid: Optional[int] = None
        try:
            pid = int(self._PIDFILE.read_text().strip())
        except (OSError, ValueError):
            pass
        if pid is not None and pid > 1:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                cmdline = b""
            if b"llama-server" in cmdline:
                logger.warning(
                    "reaping orphaned llama-server (pid %d) left by a "
                    "previous daemon", pid,
                )
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                deadline = time.time() + 10
                while time.time() < deadline and os.path.exists(f"/proc/{pid}"):
                    time.sleep(0.2)
        try:
            self._PIDFILE.unlink()
        except OSError:
            pass

        try:
            resp = self._session.get(
                f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/health", timeout=1)
        except (http_requests.ConnectionError, http_requests.Timeout):
            return  # port free — the normal case
        raise RuntimeError(
            f"port {self.LLAMA_SERVER_PORT} already answers /health "
            f"(HTTP {resp.status_code}) and the pid-file does not identify "
            f"it as ours — refusing to spawn a child that would die on bind "
            f"while the stale server passes our health checks"
        )

    def _probe_expects_thinking(self) -> bool:
        """Does this model's chat template open a <think> block for the assistant?

        If it does, the template swallows the opening tag, so the model's output
        starts mid-reasoning with only an orphan </think> to close it — and
        proxy_stream has to withhold every token until that tag arrives. If it
        does not, there is no reasoning to strip and withholding is pure harm:
        the whole response lands in one chunk when the stream ends.

        Two signals, and BOTH must say "no reasoning" before we stream:
        a literal <think> anywhere in the template, and llama.cpp's own
        supports_preserve_reasoning capability. A hybrid template (Qwen3.6)
        carries <think> behind an enable_thinking conditional, so the substring
        test reads True for it — erring toward buffering, which is the safe way
        to be wrong.

        Any failure to read /props also returns True. Withholding a response is
        recoverable; leaking reasoning is not. The client executes <<<TOOL>>>
        markers for real, so a model merely *reasoning about* a shell command
        would have that command run.
        """
        try:
            resp = self._session.get(
                f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/props", timeout=5)
            resp.raise_for_status()
            props = resp.json()
        except (http_requests.RequestException, ValueError) as e:
            logger.warning("could not read /props (%s); assuming a thinking model "
                           "and buffering the stream", e)
            return True

        # /props is advisory metadata, not a load-bearing contract: a shape we
        # don't recognise must not take the model down. Fall back, don't raise.
        if not isinstance(props, dict):
            logger.warning("/props returned %s, not an object; assuming a thinking "
                           "model and buffering the stream", type(props).__name__)
            return True

        template = props.get('chat_template')
        caps = props.get('chat_template_caps')
        template = template if isinstance(template, str) else ''
        caps = caps if isinstance(caps, dict) else {}
        expects = ('<think>' in template) or bool(caps.get('supports_preserve_reasoning'))
        logger.info(
            "chat template %s reasoning — streaming is %s",
            "uses" if expects else "does not use",
            "buffered until </think>" if expects else "incremental",
        )
        return expects

    @property
    def _slot_save_dir(self) -> Path:
        # var/ lives at the repo root; this file is at src/coding_model_server/.
        return Path(__file__).resolve().parents[2] / 'var' / 'kv_cache'

    @staticmethod
    def _slot_cache_filename(signature: tuple) -> str:
        """Save-file name keyed by the FULL runtime signature (DEV-408).

        llama-server hard-fails a restore whose model/quant/KV-type/flags
        don't match the saved state, so the key must change whenever any of
        those change — which is exactly what _runtime_signature captures.
        """
        return hashlib.sha1(repr(signature).encode()).hexdigest()[:16] + '.bin'

    def _save_slot_state(self) -> None:
        """Park the live slot's KV on disk before the child goes away.

        Best-effort by design: a failed save costs one re-prefill, while an
        error escaping here would break the swap/shutdown path — so nothing
        propagates. Skipped when the cached context is too small to be worth
        the file write.
        """
        if not self.SLOT_SAVE_ENABLED:
            return
        with self.lock:
            signature = self.current_runtime_signature
            proc = self.process
        if signature is None or proc is None or proc.poll() is not None:
            return
        base = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}"
        try:
            slots = self._session.get(f"{base}/slots", timeout=5).json()
            n_past = 0
            if isinstance(slots, list) and slots:
                # Field name has drifted across server versions — our
                # 2026-06 build reports n_prompt_tokens; older ones used
                # n_past. Fall through to 0 (skip) if none exist rather
                # than guessing.
                slot0 = slots[0]
                n_past = int(slot0.get('n_prompt_tokens')
                             or slot0.get('n_past')
                             or slot0.get('n_ctx_used') or 0)
            if n_past < self.SLOT_SAVE_MIN_TOKENS:
                return
            fname = self._slot_cache_filename(signature)
            resp = self._session.post(
                f"{base}/slots/0?action=save",
                json={"filename": fname}, timeout=self.SLOT_IO_TIMEOUT,
            )
            if resp.status_code == 200:
                logger.info("slot save: parked %d tokens of KV as %s",
                            n_past, fname)
                self._trim_slot_cache_dir()
            else:
                logger.warning("slot save failed: HTTP %d %s",
                               resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("slot save skipped: %s", e)

    def _restore_slot_state(self, signature: tuple) -> None:
        """Reload parked KV for this exact runtime signature, if any.

        A rejected or corrupt file is deleted so one bad save can't fail
        every future start of that model. Best-effort like the save side.
        """
        if not self.SLOT_SAVE_ENABLED:
            return
        fname = self._slot_cache_filename(signature)
        path = self._slot_save_dir / fname
        if not path.is_file():
            return
        base = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}"
        try:
            resp = self._session.post(
                f"{base}/slots/0?action=restore",
                json={"filename": fname}, timeout=self.SLOT_IO_TIMEOUT,
            )
            if resp.status_code == 200:
                logger.info("slot restore: reloaded KV from %s", fname)
            else:
                logger.warning(
                    "slot restore rejected (HTTP %d) — discarding %s",
                    resp.status_code, fname)
                path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("slot restore skipped: %s", e)

    def _trim_slot_cache_dir(self) -> None:
        """Drop oldest save files until the directory fits the size cap."""
        try:
            files = sorted(self._slot_save_dir.glob('*.bin'),
                           key=lambda p: p.stat().st_mtime)
            budget = self.SLOT_SAVE_MAX_TOTAL_GIB * (1024 ** 3)
            total = sum(p.stat().st_size for p in files)
            while files and total > budget:
                victim = files.pop(0)
                total -= victim.stat().st_size
                victim.unlink(missing_ok=True)
                logger.info("slot cache trim: removed %s", victim.name)
        except OSError as e:
            logger.warning("slot cache trim failed: %s", e)

    def _shutdown_unlocked(self, save_slot: bool = True):
        """Internal: stop the subprocess. Caller must hold self._swap_lock.

        Releases self.lock during the SIGTERM/wait so dashboard snapshots
        don't hang for up to 15s while we're killing the child. Lock-free
        operations only touch the local ``proc`` reference; concurrent
        readers see _state == STOPPING and self.process == None as soon as
        we transition.

        ``save_slot`` parks the live slot's KV to disk first (DEV-408) —
        passed False only on the unhealthy-child path, where the HTTP save
        would just hang against a wedged server.
        """
        if save_slot:
            self._save_slot_state()
        # Capture the process handle and clear "current" state under a
        # short lock acquisition. After this block, readers see "stopping"
        # state with no process attached; the actual SIGTERM/wait runs
        # without self.lock.
        with self.lock:
            proc = self.process
            if proc is None:
                self._state = _STATE_IDLE
                return
            self._watchdog_generation += 1  # invalidate the current watchdog
            self._state = _STATE_STOPPING
            self.process = None
            self.current_model_path = None
            self.current_model_config = None
            self.current_agent_id = None
            self.current_runtime_signature = None
            self.current_expects_thinking = True  # back to the safe default
            self.started_at = None

        pid = proc.pid

        def _signal_group(sig):
            # The child runs in its own session (start_new_session=True), so
            # the group id is its pid; killing the group takes down anything
            # it spawned too (DEV-157). Fall back to the single process for
            # children spawned before this change.
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                proc.send_signal(sig)

        logger.info("Shutting down llama-server (PID %d)...", pid)
        try:
            _signal_group(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
                logger.info("llama-server (PID %d) terminated gracefully", pid)
            except subprocess.TimeoutExpired:
                logger.warning("llama-server (PID %d) didn't exit, sending SIGKILL", pid)
                _signal_group(signal.SIGKILL)
                proc.wait(timeout=5)
        except Exception as e:
            logger.error("Error shutting down llama-server: %s", e)
        try:
            self._PIDFILE.unlink()
        except OSError:
            pass

        with self.lock:
            self._state = _STATE_IDLE

    def _gpu_total_mib(self) -> Optional[int]:
        """Total VRAM (device 0), cached — it never changes at runtime."""
        if self._gpu_total_mib_cache is not None:
            return self._gpu_total_mib_cache
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return None
            self._gpu_total_mib_cache = int(result.stdout.strip().splitlines()[0])
        except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
            return None
        return self._gpu_total_mib_cache

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
        on the architect (dense_architect, ~14 GiB resident) → fast_implementer
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

    # Coarsest keep-alive granularity worth a lock acquisition. The idle
    # watchdog's timeout is minutes, so refreshing more often than this buys
    # nothing but per-token lock traffic on the streaming hot path.
    _KEEPALIVE_THROTTLE_S = 5.0

    def _bump_last_request_time(self) -> None:
        """Refresh the idle-watchdog keep-alive from inside a stream.

        The streaming loop calls this once per emitted delta. Writing
        last_request_time there used to be done bare, off-lock — the one place
        the two-lock discipline was violated (the watchdog reads it under
        self.lock). Guard the write, but throttle it: the unlocked read is an
        atomic float load used only to skip the common case, so we take the lock
        at most once per _KEEPALIVE_THROTTLE_S rather than on every token.
        """
        now = time.time()
        if now - self.last_request_time < self._KEEPALIVE_THROTTLE_S:
            return
        with self.lock:
            self.last_request_time = now

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
        recorded its delta, subsequent loads gate on that measurement.

        The hard floor is ``free >= recorded``, and the margin on top of it is
        advisory. That asymmetry is the whole point of this function, so it is
        worth being explicit about why.

        ``recorded`` is not an estimate. It is the delta measured across a load
        that SUCCEEDED, on this GPU, in this process. So a model that fits in
        ``recorded`` MiB is not a prediction — it is a fact we observed. Demanding
        ``recorded + margin`` before allowing the same model back therefore asks
        for VRAM the load never needed, and for an agent whose footprint lands
        within ``margin`` of the card's capacity it asks for VRAM that does not
        exist on the card at all. That check cannot be satisfied by ANY amount of
        freeing, because there is nothing left to free: the GPU is already idle.

        That is not hypothetical. `implementer` at n_cpu_moe=18 loaded fine and
        left ~500 MiB free, so `recorded` came to within a margin of the whole
        card. Every RELOAD then failed — not because VRAM was scarce, but because
        the arithmetic could never come out true. The first load in a process is
        exempt (nothing recorded yet), so the server looked healthy right up until
        the idle watchdog reaped the child, at which point it bricked itself and
        stayed bricked until a human restarted it. See [[project_llama_server_child_lifecycle]].

        So: refuse only when the model genuinely does not fit (``free < recorded``
        — something else is holding VRAM we need). When it fits but the margin is
        not fully covered, load it and say so. A thinner-than-preferred cushion is
        a risk; refusing forever is a certainty.
        """
        if agent_id is None:
            return
        recorded = self._measured_vram_delta_mib.get(agent_id)
        if recorded is None:
            return
        free = self._gpu_free_mib()
        if free is None:
            return  # nvidia-smi unavailable — proceed and hope for the best

        if free < recorded:
            # DEV-156: an inflated record (another process allocated between
            # the two nvidia-smi readings) refuses FOREVER, because only a
            # successful load refreshes it. Three consecutive refusals mark
            # the record suspect: clear it and let this load attempt
            # re-measure instead of staying bricked until a daemon restart.
            refusals = self._vram_refusals.get(agent_id, 0) + 1
            if refusals >= 3:
                logger.warning(
                    "[vram] %s refused %d times in a row (recorded=%d MiB, "
                    "free=%d MiB) — record looks inflated; clearing it and "
                    "attempting the load", agent_id, refusals, recorded, free,
                )
                self._vram_refusals.pop(agent_id, None)
                self._measured_vram_delta_mib.pop(agent_id, None)
                return
            self._vram_refusals[agent_id] = refusals
            raise InsufficientVramError(
                f"refusing to load {agent_id}: needs ~{recorded} MiB "
                f"but only {free} MiB free"
            )
        self._vram_refusals.pop(agent_id, None)

        if free < recorded + self._VRAM_MARGIN_MIB:
            logger.warning(
                "[vram] %s fits with only %d MiB to spare (prefer >=%d). Loading "
                "anyway — it measured %d MiB on a successful load, so it fits. "
                "Raise n_cpu_moe for this agent if the compute buffer OOMs.",
                agent_id, free - recorded, self._VRAM_MARGIN_MIB, recorded,
            )

    def ensure_running(self, model_config: dict, agent_id: Optional[str] = None,
                       reserve_slot: bool = False):
        """Ensure llama-server is running with the correct model. Handles model swaps.

        ``agent_id`` is recorded so the dashboard can show which logical agent
        is currently bound to the loaded model. Multiple agent_ids share a
        single model path, so this is updated on every call, not only on swap.

        Swap detection compares on the full runtime signature, not just on
        ``path`` — see ``_runtime_signature`` for why path-equality wasn't
        enough. Before every (re)start, ``_check_vram_or_raise`` consults
        the per-agent measured footprint and raises ``InsufficientVramError``
        rather than letting the child crash on a cudaMalloc failure.

        ``reserve_slot=True`` counts the caller's upcoming proxy_* call in
        ``_active_requests`` before ``_swap_lock`` is released. Without the
        reservation there is a TOCTOU hole: a StreamingResponse generator
        isn't iterated until after the route returns, so a second agent's
        ensure_running saw has_active_requests()==False in that gap and
        swapped the child — the first request then POSTed to a port serving
        the other model and silently received wrong-model output (DEV-116).
        The caller owns the reserved slot and must release it exactly once
        via ``release_slot`` (and pass reserved=True to proxy_* so the proxy
        doesn't double-count).
        """
        signature = self._runtime_signature(model_config)
        # _swap_lock is held for the entire transition (potentially many
        # seconds). self.lock is acquired in short bursts inside this
        # block — readers (snapshot, is_running) see consistent state and
        # don't have to wait for the slow operations.
        with self._swap_lock:
            with self.lock:
                # Liveness is part of "already correct". A child that dies out
                # of band — CUDA crash, the OOM-killer, an external kill —
                # leaves _state == RUNNING behind, because nothing transitions
                # it. Trusting _state alone made this an early-return no-op, so
                # the manager never respawned and every later request proxied to
                # a dead port and 502'd until the unit was restarted by hand.
                # is_running() has always checked poll(); this has to agree.
                prev_state = self._state
                child_alive = self.process is not None and self.process.poll() is None
                already_correct = (
                    prev_state == _STATE_RUNNING
                    and child_alive
                    and self.current_runtime_signature == signature
                )
                if already_correct:
                    self.last_request_time = time.time()
                    if agent_id is not None:
                        self.current_agent_id = agent_id
                    if reserve_slot:
                        self._active_requests += 1
                    return
                # Tear down whenever state from a previous child is still
                # attached: alive (a genuine swap) or dead (stale state to
                # clear before starting fresh).
                need_swap = prev_state == _STATE_RUNNING or self.process is not None
                same_path = (
                    need_swap
                    and self.current_model_path == model_config.get('path')
                )

            if need_swap:
                # Refuse to swap out a LIVE child while another request is in
                # flight against it. _shutdown_unlocked() SIGTERMs the shared
                # child, which would drop that request's stream mid-token. The
                # idle watchdog already skips its kill while _active_requests > 0
                # (see _idle_watchdog); this mirrors that guard on the swap path,
                # surfacing a retryable busy signal the caller turns into a
                # 503 + Retry-After rather than a blocking wait (which would pin
                # _swap_lock for another agent's full generation).
                #
                # Gated on child_alive so it never blocks the two paths that
                # legitimately swap with a request "active":
                #   * the caller's own request is only counted AFTER this guard
                #     passes (the reserve_slot increment below) — so this sees
                #     only *other* requests: earlier reservations and in-flight
                #     proxies.
                #   * crash recovery (_post_with_recovery) reaches here only with
                #     a dead child, so child_alive is False and there is no live
                #     stream left to protect.
                if child_alive and self.has_active_requests():
                    raise ModelBusyError(
                        f"model swap for {agent_id or '?'} deferred: another "
                        f"request is in flight against the current model — retry shortly"
                    )
                # Runtime drift — even if the GGUF file is the same, n_ctx
                # / sampler bias / server flags can differ between agents
                # that share a path. Shut down and reload to honor the new
                # config. _wait_for_vram_release handles the kernel's async
                # free.
                if not child_alive:
                    logger.warning(
                        "llama-server child is gone (state=%s) — clearing stale "
                        "state and starting a fresh one", prev_state,
                    )
                elif same_path:
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
                # If a prior run of this exact runtime parked its KV, reload
                # it now (DEV-408) — the next request with a matching prefix
                # then skips its prefill entirely.
                self._restore_slot_state(signature)
                if agent_id is not None:
                    with self.lock:
                        self.current_agent_id = agent_id
                    free_after = self._gpu_free_mib()
                    if free_before is not None and free_after is not None:
                        delta = free_before - free_after
                        total = self._gpu_total_mib()
                        if total is not None and delta > total:
                            # Impossible footprint — another process allocated
                            # between the two readings (DEV-156). Recording it
                            # would guarantee eternal refusals.
                            logger.warning(
                                "[vram] %s measured delta %d MiB exceeds the "
                                "card's %d MiB — cross-process interference; "
                                "not recording", agent_id, delta, total,
                            )
                        elif delta > 0:
                            with self.lock:
                                self._measured_vram_delta_mib[agent_id] = delta
                                self._vram_refusals.pop(agent_id, None)
                            logger.info(
                                "[vram] %s footprint: %d MiB (free %d -> %d)",
                                agent_id, delta, free_before, free_after,
                            )
                            # Say this at the load that CAUSES it, not 30 minutes
                            # later. An agent this tight loads fine and serves fine;
                            # the cost only shows up under memory pressure or on a
                            # compute-buffer growth during prefill, long after the
                            # config that caused it stopped being the obvious suspect.
                            if free_after < self._VRAM_MARGIN_MIB:
                                logger.warning(
                                    "[vram] %s leaves only %d MiB free (prefer >=%d). "
                                    "It will still reload — the guard gates on the "
                                    "measured footprint, not on the margin — but there "
                                    "is no cushion for compute-buffer growth or another "
                                    "process. Raise n_cpu_moe to buy headroom.",
                                    agent_id, free_after, self._VRAM_MARGIN_MIB,
                                )
            except Exception as e:
                logger.error("Failed to start llama-server for %s: %s",
                             model_config.get('path'), e)
                # start() / _shutdown_unlocked already cleared state on
                # their failure paths; re-raise so the caller gets the
                # original exception type for proper error categorization.
                raise

            # Reserve before _swap_lock is released so no other agent's
            # ensure_running can sneak a swap in between "the right model is
            # up" and the caller's proxy_* actually POSTing to it.
            if reserve_slot:
                with self.lock:
                    self._active_requests += 1

    def release_slot(self):
        """Release a slot reserved by ``ensure_running(reserve_slot=True)``.

        Exactly-once discipline is the caller's job (the chat route wraps
        this in an idempotent teardown hook); the zero floor only stops a
        stray release from going negative and wedging the busy guard open.
        """
        with self.lock:
            self.last_request_time = time.time()
            if self._active_requests > 0:
                self._active_requests -= 1

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
        """Start a watchdog for the child that just came up.

        Always starts a fresh thread bound to a new generation — never
        early-return on "a thread is still alive". The alive thread may
        belong to the previous child and be one wake away from seeing its
        stale generation and exiting; reusing it silently left the new
        child unwatched (DEV-118). The superseded thread exits at its next
        wake (≤30s), so the overlap is bounded and harmless.
        """
        with self.lock:
            self._watchdog_generation += 1
            gen = self._watchdog_generation
        self._watchdog_thread = Thread(
            target=self._idle_watchdog, args=(gen,), daemon=True)
        self._watchdog_thread.start()

    def _idle_watchdog(self, gen: int):
        """Background thread: shut down subprocess if idle for IDLE_TIMEOUT seconds.

        ``gen`` pins this thread to one child's lifetime: the moment a
        shutdown or a newer watchdog bumps the generation, this thread's
        next wake exits without touching state that now belongs to a
        successor.

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
        while True:
            time.sleep(30)  # Check every 30s
            with self.lock:
                if gen != self._watchdog_generation:
                    break  # superseded — a newer child owns the watchdog now
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
        resp = self._session.post(url, json={"content": text}, timeout=5)
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
                return self._session.post(
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
                               parallel_tool_calls: Optional[bool],
                               chat_template_kwargs: Optional[Dict[str, Any]] = None) -> dict:
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
        # Jinja template variables — e.g. {'enable_thinking': False} against a
        # hybrid template (DEV-556). Sent only when non-empty, so a model
        # without --jinja and every agent that has not opted in produce exactly
        # the payload they produced before this parameter existed.
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        return payload

    def proxy_stream(self, messages: List[dict], system_prompt: str,
                     model_id: str, max_tokens: int, temperature: float,
                     model_config: dict = None,
                     est_prompt_tokens: int = 0,
                     tools: Optional[List[Dict[str, Any]]] = None,
                     tool_choice: Optional[Any] = None,
                     parallel_tool_calls: Optional[bool] = None,
                     chat_template_kwargs: Optional[Dict[str, Any]] = None,
                     req_id: Optional[str] = None,
                     reserved: bool = False,
                     upstream_cancel: Optional[UpstreamCancel] = None) -> Iterator[str]:
        """Stream a chat completion via the llama-server subprocess.

        ``upstream_cancel`` (DEV-158): the route registers the live upstream
        response on it so its disconnect watcher can close the socket the
        moment the client goes away, instead of waiting for the next token.

        ``req_id`` is the correlation ID assigned by the metrics middleware;
        we prefix it onto every log line so a grep on `req_xxxxxxxx` shows
        the full lifecycle of one request from entry through proxy to
        upstream errors.

        ``reserved=True`` means the caller already holds this request's
        _active_requests slot (ensure_running(reserve_slot=True)) and owns
        its release — the proxy neither increments nor decrements. That
        matters for a generator: this body doesn't run until first
        iteration, so a finally-based decrement here would never fire for a
        stream cancelled before it starts, leaking the count (DEV-116).
        """
        rid = req_id or "req_unknown"
        with self.lock:
            self.last_request_time = time.time()
            if not reserved:
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
                chat_template_kwargs,
            )

            completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            finish_reason = None
            accumulated_text = []  # Diagnostic: capture full response
            think_stripper = ThinkingStripper(
                expect_thinking=self.current_expects_thinking)

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
                if upstream_cancel is not None:
                    upstream_cancel.register(resp)
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
                                self._bump_last_request_time()
                                out_chunk = build_stream_chunk(completion_id, model_id, tool_calls=tc)
                                yield f"data: {json.dumps(out_chunk)}\n\n"

                            if content:
                                accumulated_text.append(content)
                                # Strip <think>...</think> blocks from streaming output
                                filtered = think_stripper.feed(content)
                                if filtered:
                                    self._bump_last_request_time()  # Keep watchdog at bay during long streams
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

            # Flush the stripper with the truncation verdict. Only a clean
            # finish ("stop", "tool_calls") may emit an unterminated think
            # buffer as content; "length" or a stream that ended with no
            # finish_reason at all means that buffer is raw reasoning — which
            # can carry tool markers the client executes for real (DEV-119).
            remaining = think_stripper.flush(
                truncated=finish_reason not in ("stop", "tool_calls"))
            if remaining:
                out_chunk = build_stream_chunk(completion_id, model_id, content=remaining)
                yield f"data: {json.dumps(out_chunk)}\n\n"
            if think_stripper.dropped_chars:
                logger.warning(
                    "[%s] suppressed %d chars of unterminated reasoning "
                    "(finish_reason=%s, no </think> seen)",
                    rid, think_stripper.dropped_chars, finish_reason,
                )

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
                if not reserved:
                    self._active_requests -= 1

    def proxy_sync(self, messages: List[dict], system_prompt: str,
                   model_id: str, max_tokens: int, temperature: float,
                   model_config: dict = None,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   tool_choice: Optional[Any] = None,
                   parallel_tool_calls: Optional[bool] = None,
                   chat_template_kwargs: Optional[Dict[str, Any]] = None,
                   req_id: Optional[str] = None,
                   reserved: bool = False) -> dict:
        """Synchronous chat completion via the llama-server subprocess.

        ``reserved`` — see proxy_stream: the caller already holds this
        request's _active_requests slot and owns its release.
        """
        rid = req_id or "req_unknown"
        with self.lock:
            self.last_request_time = time.time()

        openai_messages = self._build_openai_messages(messages, system_prompt)
        url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/v1/chat/completions"
        payload = self._build_request_payload(
            openai_messages, max_tokens, temperature, False,
            model_config, tools, tool_choice, parallel_tool_calls,
            chat_template_kwargs,
        )

        with self.lock:
            if not reserved:
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
            finish_reason = result["choices"][0].get("finish_reason", "stop")
            if (self.current_expects_thinking and finish_reason == "length"
                    and '</think>' not in text):
                # Truncated while still thinking: the whole body is raw
                # reasoning. An orphan-template model emits no literal tags
                # in this state, so strip_thinking would pass every char
                # through — including tool markers the client executes for
                # real (DEV-119).
                logger.warning(
                    "[%s] response hit max_tokens mid-reasoning — suppressing "
                    "%d chars of unterminated thinking", rid, len(text),
                )
                text = ""
            else:
                text = strip_thinking(text)
            tool_calls = message.get("tool_calls")
            usage = result.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

            with self.lock:
                self.last_request_time = time.time()
            return build_completion_response(model_id, text, usage,
                                             finish_reason=finish_reason,
                                             tool_calls=tool_calls)
        finally:
            with self.lock:
                if not reserved:
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

