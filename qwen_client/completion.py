"""Server completion requests — streaming, retries, context management."""
import sys
import json
import time
import logging
import threading

import requests
import urllib3

from qwen_client.config import config, COLORS, print_colored

logger = logging.getLogger(__name__)


def wait_for_server():
    """Poll server health endpoint until it comes back online."""
    print_colored(f"\nConnection lost. Waiting for server at {config.LINUX_SERVER_IP}...", COLORS['WARNING'])
    while True:
        try:
            response = requests.get(config.HEALTH_URL, timeout=config.REQUEST_TIMEOUT)
            if response.status_code == 200:
                print_colored("\nServer is back online! Resuming...", COLORS['GREEN'])
                return True
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            pass
        try:
            time.sleep(5)
            print(".", end="", flush=True)
        except KeyboardInterrupt:
            print_colored("\nPolling cancelled by user.", COLORS['FAIL'])
            return False


# ---------------------------------------------------------------------------
# History helpers (used only by get_completion)
# ---------------------------------------------------------------------------

def _trim_history_for_context(history):
    """Trim history when context limit is reached.

    Preserves history[0] if it's a system prompt — without this, repeated
    trims under context pressure eventually evict the agent's role
    instructions and the model loses its contract.
    """
    if len(history) > 2:
        sys_offset = 1 if history[0].get("role") == "system" else 0
        trim_index = max(sys_offset + 1, int(len(history) * 0.25))
        while trim_index < len(history) - 1 and history[trim_index]["role"] != "user":
            trim_index += 1
        trimmed_past = history[trim_index:-1]
        history[:] = history[:sys_offset] + trimmed_past + [history[-1]]

        sanitized_retry = []
        valid_roles = {"system", "user", "assistant"}
        for m in history:
            content = m.get("content", "").strip()
            role = m.get("role", "")
            if content and role in valid_roles:
                sanitized_retry.append({"role": role, "content": content})
        if not sanitized_retry:
            sanitized_retry.append({"role": "user", "content": "Hello"})
        return sanitized_retry
    return None


def _compress_history(messages, keep_recent=6, summary_len=200):
    """Compress older messages to reduce context usage.

    Keeps the last ``keep_recent`` messages at full fidelity.
    Older tool-output messages (>500 chars) and large assistant messages
    (>2000 chars) are truncated to head + tail summaries.
    """
    if len(messages) <= keep_recent:
        return messages

    compressed = []
    cutoff = len(messages) - keep_recent

    for i, msg in enumerate(messages):
        if i < cutoff:
            content = msg.get("content") or ""
            is_tool_output = (
                content.startswith("Tool output:\n")
                or msg.get("role") == "tool"
            )
            threshold = 500 if is_tool_output else 2000

            if len(content) > threshold:
                head = content[:summary_len]
                tail = content[-summary_len:]
                msg = {**msg, "content": f"{head}\n... [truncated, was {len(content)} chars] ...\n{tail}"}
        compressed.append(msg)

    return compressed


# ---------------------------------------------------------------------------
# Main completion function
# ---------------------------------------------------------------------------

def get_completion(history, model, agent_theme, agentic_context=None,
                   tools=None, tool_choice=None):
    """Get a streaming completion from the server.

    Returns ``(response_text, finish_reason, tool_calls)`` on success, or
    ``(None, None, None)`` on failure.  ``finish_reason`` is ``"stop"`` for
    normal completion, ``"length"`` for truncation, ``"tool_calls"`` when
    the model emitted a native tool invocation.

    ``tool_calls`` is a list of OpenAI-shape dicts (``{id, type, function:{
    name, arguments}}``) reassembled from streaming deltas, or ``None`` when
    the model did not call any tools.

    If *agentic_context* is provided it is appended as an additional user
    message in the **sanitized copy only** — the actual *history* list is
    never modified.
    """
    full_response = ""
    finish_reason = "stop"
    server_error_occurred = False
    tool_calls_acc = {}  # index -> {id, type, function:{name, arguments}}

    # Sanitize history — strip internal flags, filter invalid messages.
    # "tool" messages are valid OpenAI roles for tool-result turns; assistant
    # messages may carry only tool_calls (empty content), so do not require
    # content for assistant or tool turns.
    sanitized_history = []
    valid_roles = {"system", "user", "assistant", "tool"}

    for msg in history:
        role = msg.get("role", "")
        if role not in valid_roles:
            continue
        content = (msg.get("content") or "").strip()
        has_tool_calls = bool(msg.get("tool_calls"))
        # Drop empty messages unless they carry tool_calls (assistant) or
        # are a tool-result turn (which always needs to ride along even if
        # the command had empty stdout — content="(no output)" upstream).
        if not content and not has_tool_calls and role != "tool":
            continue
        out = {"role": role, "content": content}
        if has_tool_calls:
            out["tool_calls"] = msg["tool_calls"]
            if not content:
                out["content"] = None
        if msg.get("tool_call_id"):
            out["tool_call_id"] = msg["tool_call_id"]
        if msg.get("name"):
            out["name"] = msg["name"]
        sanitized_history.append(out)

    if not sanitized_history:
        sanitized_history.append({"role": "user", "content": "Hello"})

    # Compress older messages
    sanitized_history = _compress_history(sanitized_history)

    # Inject agentic context (scratchpad, plan, budget warnings, etc.)
    if agentic_context:
        sanitized_history.append({"role": "user", "content": agentic_context})

    payload = {
        "model": model,
        "messages": sanitized_history,
        "stream": True,
        "max_tokens": 30000,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    # Progress tracking
    start_time = time.time()
    first_token_time = None
    chunk_count = 0
    stop_progress = threading.Event()
    progress_info = {}  # Populated by server's progress SSE event

    def show_progress():
        last_heartbeat = time.time()
        while not stop_progress.is_set():
            now = time.time()
            elapsed = now - start_time
            if now - last_heartbeat > 30:
                try:
                    requests.get(config.HEALTH_URL, timeout=2)
                    last_heartbeat = now
                except Exception:
                    pass
            if progress_info:
                prompt_k = progress_info['prompt_tokens'] / 1000
                ctx_k = progress_info['n_ctx'] / 1000
                sys.stdout.write(
                    f"\r{COLORS['BLUE']}Prefill: {prompt_k:.1f}K / {ctx_k:.0f}K tokens — "
                    f"{model} ({elapsed:.1f}s){COLORS['ENDC']}")
            else:
                sys.stdout.write(f"\r{COLORS['BLUE']}Waiting for {model}... ({elapsed:.1f}s){COLORS['ENDC']}")
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

    progress_thread = threading.Thread(target=show_progress, daemon=True)
    progress_thread.start()

    context_retries = 0
    MAX_CONTEXT_RETRIES = 3
    TTFT_TIMEOUT = 600

    def _is_context_error(text):
        return any(phrase in text for phrase in (
            "exceed context window", "context_length_exceeded",
            "fills the entire context window",
        ))

    def _try_trim_and_retry():
        nonlocal context_retries
        if context_retries < MAX_CONTEXT_RETRIES:
            context_retries += 1
            print_colored(
                f"\n[Client] Context limit likely reached. Trimming history and retrying "
                f"({context_retries}/{MAX_CONTEXT_RETRIES})...",
                COLORS['WARNING']
            )
            sanitized_retry = _trim_history_for_context(history)
            if sanitized_retry:
                payload["messages"] = sanitized_retry
                return True
        return False

    while True:
        try:
            response = requests.post(config.API_URL, json=payload, headers=config.auth_headers, stream=True, timeout=7200)

            if response.status_code != 200:
                stop_progress.set()
                error_text = response.text
                if _is_context_error(error_text) and _try_trim_and_retry():
                    stop_progress = threading.Event()
                    progress_thread = threading.Thread(target=show_progress, daemon=True)
                    progress_thread.start()
                    continue
                print_colored(f"\nError: {error_text}", COLORS['FAIL'])
                return None, None, None

            # ── TTFT watchdog ──
            # iter_lines() blocks during the entire prefill stage waiting for the
            # next chunk from the server, so the in-loop TTFT check below is
            # unreachable while the server is stalled.  This independent watchdog
            # thread polls elapsed time and forcibly closes the response if the
            # first content token doesn't arrive within TTFT_TIMEOUT seconds.
            ttft_stalled = threading.Event()
            req_start = start_time

            def _ttft_watchdog(resp):
                deadline = req_start + TTFT_TIMEOUT
                while time.time() < deadline:
                    if first_token_time is not None or stop_progress.is_set():
                        return
                    time.sleep(2)
                if first_token_time is None and not stop_progress.is_set():
                    ttft_stalled.set()
                    try:
                        resp.close()
                    except Exception:
                        pass

            watchdog_thread = threading.Thread(
                target=_ttft_watchdog, args=(response,), daemon=True
            )
            watchdog_thread.start()

            try:
                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                if data.get("type") == "progress":
                                    progress_info.update(data)
                                    continue
                                if "choices" in data and len(data["choices"]) > 0:
                                    choice = data["choices"][0]
                                    delta = choice.get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        if not first_token_time:
                                            first_token_time = time.time()
                                            stop_progress.set()
                                        print(content, end="", flush=True)
                                        full_response += content
                                        chunk_count += 1
                                    # Native tool_calls arrive as indexed deltas:
                                    # {index, id?, type?, function:{name?, arguments?}}.
                                    # Reassemble by index so parallel calls stay separate.
                                    for tc in delta.get("tool_calls") or []:
                                        if not first_token_time:
                                            first_token_time = time.time()
                                            stop_progress.set()
                                        idx = tc.get("index", 0)
                                        slot = tool_calls_acc.setdefault(
                                            idx, {"id": "", "type": "function",
                                                  "function": {"name": "", "arguments": ""}}
                                        )
                                        if "id" in tc:
                                            slot["id"] = tc["id"]
                                        if "type" in tc:
                                            slot["type"] = tc["type"]
                                        fn = tc.get("function", {})
                                        if "name" in fn:
                                            slot["function"]["name"] += fn["name"]
                                        if "arguments" in fn:
                                            slot["function"]["arguments"] += fn["arguments"]
                                        chunk_count += 1
                                    if choice.get("finish_reason"):
                                        finish_reason = choice["finish_reason"]
                                elif "error" in data:
                                    stop_progress.set()
                                    error_msg = data['error'].get('message', 'Unknown error')
                                    if _is_context_error(error_msg) and _try_trim_and_retry():
                                        server_error_occurred = True
                                        break
                                    print_colored(f"\nServer Error: {error_msg}", COLORS['FAIL'])
                                    finish_reason = "error"
                                    break
                            except json.JSONDecodeError:
                                pass  # Malformed SSE chunk — skip
                            except Exception as e:
                                logger.debug("SSE parse error: %s", e)
            except KeyboardInterrupt:
                stop_progress.set()
                try:
                    response.close()
                except Exception:
                    pass
                if full_response:
                    print_colored("\n[Interrupted] Keeping partial response.", COLORS['WARNING'])
                    finish_reason = "interrupted"
                    break
                else:
                    return None, None, None
            except (requests.exceptions.ChunkedEncodingError, urllib3.exceptions.ProtocolError):
                # Watchdog killed the connection on TTFT timeout — abort cleanly
                # rather than retrying (the underlying issue is server-side).
                if ttft_stalled.is_set():
                    stop_progress.set()
                    elapsed = time.time() - start_time
                    print_colored(
                        f"\n[Client] TTFT exceeded {TTFT_TIMEOUT}s "
                        f"(elapsed {elapsed:.0f}s) — server stalled in prefill.\n"
                        "  Likely causes: memory pressure / swap thrashing, GPU thermal\n"
                        "  throttling, or another process eating resources.\n"
                        "  Check `top`, `free -h`, and `nvidia-smi` on the server.",
                        COLORS['FAIL']
                    )
                    return None, None, None
                # Real connection drop — bubble to outer handler which retries
                raise

            if server_error_occurred and context_retries <= MAX_CONTEXT_RETRIES:
                server_error_occurred = False
                full_response = ""
                chunk_count = 0
                start_time = time.time()
                first_token_time = None
                stop_progress = threading.Event()
                progress_thread = threading.Thread(target=show_progress, daemon=True)
                progress_thread.start()
                continue
            break

        except requests.exceptions.ConnectionError:
            stop_progress.set()
            if chunk_count > 0:
                break
            if wait_for_server():
                start_time = time.time()
                stop_progress = threading.Event()
                progress_thread = threading.Thread(target=show_progress, daemon=True)
                progress_thread.start()
                continue
            return None, None, None
        except (requests.exceptions.ChunkedEncodingError, urllib3.exceptions.ProtocolError) as e:
            stop_progress.set()
            print_colored(f"\n[Client] Connection interrupted: {e}. Retrying...", COLORS['WARNING'])
            if chunk_count > 0:
                full_response = ""
                chunk_count = 0
            start_time = time.time()
            stop_progress = threading.Event()
            progress_thread = threading.Thread(target=show_progress, daemon=True)
            progress_thread.start()
            continue
        except Exception as e:
            stop_progress.set()
            print_colored(f"\nUnexpected error: {e}", COLORS['FAIL'])
            return None, None, None

    stop_progress.set()
    print()
    if chunk_count > 0:
        end_time = time.time()
        total_duration = end_time - start_time
        ttft = first_token_time - start_time
        gen_duration = end_time - first_token_time
        cps = chunk_count / gen_duration if gen_duration > 0 else 0
        print_colored(
            f"[Stats] {model}: TTFT: {ttft:.2f}s | Total: {total_duration:.2f}s | "
            f"{chunk_count} chunks | {cps:.2f} chunks/s",
            COLORS['CYAN']
        )

    if finish_reason == "length":
        print_colored("[Truncated] Response hit token limit — continuation needed.", COLORS['WARNING'])

    tool_calls_list = (
        [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        if tool_calls_acc else None
    )
    return full_response, finish_reason, tool_calls_list
