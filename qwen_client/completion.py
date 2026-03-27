"""Server completion requests — streaming, retries, context management."""
import sys
import json
import time
import threading

import requests
import urllib3

from qwen_client.config import config, COLORS, print_colored


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
    """Trim history when context limit is reached."""
    if len(history) > 2:
        trim_index = max(1, int(len(history) * 0.25))
        while trim_index < len(history) - 1 and history[trim_index]["role"] != "user":
            trim_index += 1
        trimmed_past = history[trim_index:-1]
        history[:] = trimmed_past + [history[-1]]

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
            content = msg["content"]
            is_tool_output = content.startswith("Tool output:\n")
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

def get_completion(history, model, agent_theme, agentic_context=None):
    """Get a streaming completion from the server.

    Returns ``(response_text, finish_reason)`` on success, or
    ``(None, None)`` on failure.  ``finish_reason`` is ``"stop"`` for
    normal completion, ``"length"`` for truncation.

    If *agentic_context* is provided it is appended as an additional user
    message in the **sanitized copy only** — the actual *history* list is
    never modified.
    """
    full_response = ""
    finish_reason = "stop"
    server_error_occurred = False

    # Sanitize history — strip internal flags, filter invalid messages
    sanitized_history = []
    valid_roles = {"system", "user", "assistant"}

    for msg in history:
        content = msg.get("content", "").strip()
        role = msg.get("role", "")
        if content and role in valid_roles:
            sanitized_history.append({"role": role, "content": content})

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

    # Progress tracking
    start_time = time.time()
    first_token_time = None
    chunk_count = 0
    stop_progress = threading.Event()

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
            sys.stdout.write(f"\r{COLORS['BLUE']}Waiting for {model}... ({elapsed:.1f}s){COLORS['ENDC']}")
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * 50 + "\r")
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
            response = requests.post(config.API_URL, json=payload, stream=True, timeout=7200)

            if response.status_code != 200:
                stop_progress.set()
                error_text = response.text
                if _is_context_error(error_text) and _try_trim_and_retry():
                    stop_progress = threading.Event()
                    progress_thread = threading.Thread(target=show_progress, daemon=True)
                    progress_thread.start()
                    continue
                print_colored(f"\nError: {error_text}", COLORS['FAIL'])
                return None, None

            try:
                for line in response.iter_lines():
                    if not first_token_time and (time.time() - start_time) > TTFT_TIMEOUT:
                        stop_progress.set()
                        response.close()
                        if _try_trim_and_retry():
                            server_error_occurred = True
                            break
                        print_colored(
                            f"\n[Client] TTFT timeout ({TTFT_TIMEOUT}s) — server not responding.",
                            COLORS['FAIL']
                        )
                        return None, None

                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
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
                            except Exception:
                                pass
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
                    return None, None

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
            return None, None
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
            return None, None

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

    return full_response, finish_reason
