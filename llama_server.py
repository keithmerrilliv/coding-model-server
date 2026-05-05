"""LlamaServerManager: subprocess lifecycle + HTTP proxy for llama-server.

Extracted from server.py for organization. Owns the single llama-server
child process used by all agents — only one model is loaded at a time;
switching agents triggers a model swap.
"""
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from threading import Lock, Thread
from typing import Any, Dict, Iterator, List, Optional

import requests as http_requests
from fastapi import HTTPException

from config import Config
from streaming import (
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

    # GGML type enum → llama-server --cache-type string
    _CACHE_TYPE_NAMES = {
        0: 'f32', 1: 'f16', 2: 'q4_0', 3: 'q4_1',
        6: 'q5_0', 7: 'q5_1', 8: 'q8_0', 9: 'q8_1',
    }

    def __init__(self):
        self.lock = Lock()
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
        self.last_request_time: float = 0
        # Number of in-flight requests. Watchdog skips the kill while >0.
        # Race-free under CPython's GIL for ±1 increments.
        self._active_requests: int = 0
        self._watchdog_thread: Optional[Thread] = None
        self._watchdog_running = False

    def start(self, model_config: dict):
        """Spawn llama-server with the given model config, wait for /health."""
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

        logger.info("Starting llama-server: %s", ' '.join(cmd))
        self.process = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
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
        drain_thread = Thread(target=_drain_output, args=(self.process,), daemon=True)
        drain_thread.start()

        # Poll /health until ready
        health_url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/health"
        deadline = time.time() + self.HEALTH_TIMEOUT
        while time.time() < deadline:
            # Check if process died
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited with code {self.process.returncode} during startup"
                )
            try:
                resp = http_requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    logger.info("llama-server healthy after %.1fs", time.time() - (deadline - self.HEALTH_TIMEOUT))
                    self.last_request_time = time.time()
                    self._start_watchdog()
                    return
            except (http_requests.ConnectionError, http_requests.Timeout):
                # Both are normal during startup: refused-connection while
                # the server isn't listening yet, then read-timeout if it
                # accepts but isn't ready. Without catching Timeout here the
                # exception escapes the loop, bypasses _shutdown_unlocked,
                # and leaks the subprocess to the caller.
                pass
            time.sleep(self.HEALTH_POLL_INTERVAL)

        # Timeout — kill the process
        self._shutdown_unlocked()
        raise TimeoutError(f"llama-server did not become healthy within {self.HEALTH_TIMEOUT}s")

    def _shutdown_unlocked(self):
        """Internal: stop the subprocess. Caller must hold self.lock or ensure exclusivity."""
        if self.process is None:
            return

        self._watchdog_running = False
        pid = self.process.pid
        logger.info("Shutting down llama-server (PID %d)...", pid)
        try:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
                logger.info("llama-server (PID %d) terminated gracefully", pid)
            except subprocess.TimeoutExpired:
                logger.warning("llama-server (PID %d) didn't exit, sending SIGKILL", pid)
                self.process.kill()
                self.process.wait(timeout=5)
        except Exception as e:
            logger.error("Error shutting down llama-server: %s", e)
        finally:
            self.process = None
            self.current_model_path = None
            self.current_model_config = None
            self.current_agent_id = None
            self.current_runtime_signature = None
            self.started_at = None

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
        """Stop the subprocess gracefully, then force-kill if needed. Thread-safe."""
        with self.lock:
            self._shutdown_unlocked()

    def is_running(self) -> bool:
        """True if a llama-server subprocess is currently alive."""
        with self.lock:
            return self.process is not None and self.process.poll() is None

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

    def ensure_running(self, model_config: dict, agent_id: Optional[str] = None):
        """Ensure llama-server is running with the correct model. Handles model swaps.

        ``agent_id`` is recorded so the dashboard can show which logical agent
        is currently bound to the loaded model. Multiple agent_ids share a
        single model path, so this is updated on every call, not only on swap.

        Swap detection compares on the full runtime signature, not just on
        ``path`` — see ``_runtime_signature`` for why path-equality wasn't
        enough.
        """
        signature = self._runtime_signature(model_config)
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                if self.current_runtime_signature == signature:
                    # Already running with the right runtime
                    self.last_request_time = time.time()
                    if agent_id is not None:
                        self.current_agent_id = agent_id
                    return
                # Runtime drift — even if the GGUF file is the same, n_ctx
                # / sampler bias / server flags can differ between agents
                # that share a path. Shut down and reload to honor the new
                # config. _wait_for_vram_release handles the kernel's async
                # free.
                if self.current_model_path == model_config.get('path'):
                    logger.info(
                        "Runtime swap (same model file, different config) for %s",
                        agent_id or '?',
                    )
                else:
                    logger.info("Model swap: shutting down llama-server for new model")
                self._shutdown_unlocked()
                self._wait_for_vram_release()

            try:
                self.start(model_config)
                if agent_id is not None:
                    self.current_agent_id = agent_id
            except Exception as e:
                logger.error("Failed to start llama-server for %s: %s",
                             model_config.get('path'), e)
                self.current_model_path = None
                self.current_agent_id = None
                self.current_model_config = None
                self.current_runtime_signature = None
                self.started_at = None
                raise

    def snapshot(self) -> dict:
        """Return a JSON-safe view of the current llama-server state.

        Used by the dashboard's ActiveModelCard. Captures the configuration
        knobs that meaningfully change inference behavior (KV quant, ngl,
        ub, cpu_moe, draft); skips internals (env, server_extra_args) since
        they're not actionable at the dashboard level.
        """
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            if not running:
                return {
                    'running': False,
                    'agent_id': None,
                    'model_path': None,
                    'model_basename': None,
                    'pid': None,
                    'idle_timeout_s': self.IDLE_TIMEOUT,
                    'active_requests': self._active_requests,
                    'last_request_seconds_ago': None,
                    'uptime_seconds': None,
                    'config': None,
                }

            cfg = self.current_model_config or {}
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
                'running': True,
                'agent_id': self.current_agent_id,
                'model_path': self.current_model_path,
                'model_basename': os.path.basename(self.current_model_path or '') or None,
                'pid': self.process.pid if self.process else None,
                'idle_timeout_s': self.IDLE_TIMEOUT,
                'active_requests': self._active_requests,
                'last_request_seconds_ago': (
                    int(now - self.last_request_time) if self.last_request_time else None
                ),
                'uptime_seconds': int(now - self.started_at) if self.started_at else None,
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
        """
        while self._watchdog_running:
            time.sleep(30)  # Check every 30s
            if not self._watchdog_running:
                break
            with self.lock:
                if self.process is None:
                    break
                if self._active_requests > 0:
                    # In-flight request — don't shut down. Treat as activity.
                    self.last_request_time = time.time()
                    continue
                idle = time.time() - self.last_request_time
                if idle >= self.IDLE_TIMEOUT:
                    logger.info("llama-server idle for %.0fs, shutting down to free resources", idle)
                    self._shutdown_unlocked()
                    break

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
                     parallel_tool_calls: Optional[bool] = None) -> Iterator[str]:
        """Stream a chat completion via the llama-server subprocess."""
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
                with http_requests.post(url, json=payload, stream=True,
                                        timeout=Config.LLAMA_SERVER_REQUEST_TIMEOUT) as resp:
                    if resp.status_code != 200:
                        # Log full error server-side; return a generic message
                        # to the client. The raw error body can leak model paths,
                        # llama-server version, library internals, and OS errors.
                        request_id = uuid.uuid4().hex[:8]
                        logger.error(
                            "llama-server returned %d (rid=%s): %s",
                            resp.status_code, request_id, resp.text,
                        )
                        error_chunk = {"error": {
                            "message": f"upstream inference error (request_id={request_id})",
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
                logger.error("Error proxying stream from llama-server: %s", e, exc_info=True)
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
            logger.info("llama-server proxy response (%d chars): %s",
                         len(full_text), repr(full_text[:2000]))

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
                   parallel_tool_calls: Optional[bool] = None) -> dict:
        """Synchronous chat completion via the llama-server subprocess."""
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
            resp = http_requests.post(url, json=payload, timeout=Config.LLAMA_SERVER_REQUEST_TIMEOUT)
            if resp.status_code != 200:
                request_id = uuid.uuid4().hex[:8]
                logger.error(
                    "llama-server returned %d (rid=%s): %s",
                    resp.status_code, request_id, resp.text,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"upstream inference error (request_id={request_id})",
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

