"""Agent task orchestrator — the core tool-calling loop."""
import json
import os
import re
import threading

from qwen_client.config import COLORS, HISTORY_CHAR_BUDGET, print_colored
from qwen_client.display import set_terminal_title, send_macos_notification
from qwen_client.models import AGENT_THEMES
from qwen_client.history import save_chat_history
from qwen_client.completion import get_completion
from qwen_client.compaction import compact_conversation
from qwen_client.agentic.context import AgenticContext
from tool_handlers import reset_write_counts

# Lazy-bound references set by main.py after tool_handlers.configure()
_process_remote_commands = None
_extract_fallback_commands = None
_execute_remote_command = None


def set_tool_functions(process_fn, extract_fn, execute_fn):
    """Called once from main.py after tool_handlers.configure()."""
    global _process_remote_commands, _extract_fallback_commands, _execute_remote_command
    _process_remote_commands = process_fn
    _extract_fallback_commands = extract_fn
    _execute_remote_command = execute_fn


# ---------------------------------------------------------------------------
# Native tool-calling (prototype)
# ---------------------------------------------------------------------------
# When QWEN_NATIVE_TOOLS=1 and the active model is in NATIVE_TOOLS_AGENTS,
# we send an OpenAI `tools` array with the request and dispatch any returned
# `tool_calls` instead of relying on <<<TAG>>> marker parsing. Only
# REMOTE_EXEC has been migrated; every other tool still flows through markers.
NATIVE_TOOLS_AGENTS = {"glm"}

_REMOTE_EXEC_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remote_exec",
        "description": (
            "Execute a shell command on the user's machine and return its "
            "combined stdout/stderr. Use for filesystem inspection, builds, "
            "tests, git operations, and other shell tasks. Do NOT use this "
            "for writing files — use the marker-based EDIT_FILE / WRITE_FILE "
            "tools for edits so diff preview and write-loop detection work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The exact shell command to execute.",
                },
            },
            "required": ["command"],
        },
    },
}


def _native_tools_for(model: str):
    """Return the native tools list for *model*, or None to disable.

    Gated on QWEN_NATIVE_TOOLS=1 and the agent being in NATIVE_TOOLS_AGENTS.
    """
    if os.environ.get("QWEN_NATIVE_TOOLS") != "1":
        return None
    if model not in NATIVE_TOOLS_AGENTS:
        return None
    return [_REMOTE_EXEC_TOOL_SCHEMA]


def _dispatch_native_tool_calls(tool_calls, agentic_ctx):
    """Run each tool_call, return list of (tool_call_id, name, result) triples.

    Only ``remote_exec`` is wired in this prototype; unknown names return an
    error string so the model can recover. Increments the agentic budget once
    per tool call to mirror marker-path accounting.
    """
    results = []
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError as e:
            results.append((tc_id, name,
                            f"ERROR: malformed JSON arguments ({e}). Raw: {args_raw[:200]}"))
            continue

        if name == "remote_exec":
            command = (args.get("command") or "").strip()
            if not command:
                results.append((tc_id, name, "ERROR: empty 'command' argument"))
                continue
            print_colored(f"\n[native tool] remote_exec: {command}", COLORS['CYAN'])
            output = _execute_remote_command(command)
            agentic_ctx.budget.increment()
            results.append((tc_id, name, output if output is not None else "(no output)"))
        else:
            results.append((tc_id, name,
                            f"ERROR: unknown tool '{name}'. Only remote_exec is wired for native tool-calling; "
                            f"use marker-based tools (<<<EDIT_FILE>>>, <<<WRITE_FILE>>>, etc.) for everything else."))
    return results


# ---------------------------------------------------------------------------
# Pending-task queue for interrupted multi-agent chains
# ---------------------------------------------------------------------------
PENDING_TASKS = []
_pending_tasks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------
MAX_STALL_NUDGES = 2

_STALL_PATTERNS = re.compile(
    r'(?:'
    r'(?:let me|I need to|I should|I will|I\'ll|now I\'ll|next I\'ll)\s'
    r'|(?:^|\n)\s*(?:let me (?:now |also )?(?:check|read|search|write|implement|create|update|fix|modify|look|open|see|try))'
    r'|(?:^|\n)\s*(?:Now (?:let me|I\'ll|I need to|I should))'
    r'|(?:here\'s (?:my|the) (?:plan|approach|strategy))'
    r'|(?:steps? (?:to|I\'ll|we\'ll|needed))'
    r')',
    re.IGNORECASE | re.MULTILINE
)

_DONE_PATTERNS = re.compile(
    r'(?:'
    r'(?:implementation is (?:now )?complete|I\'ve (?:finished|completed|implemented|done))'
    r'|(?:the (?:changes|implementation|fix|update|code) (?:is|are) (?:now )?(?:complete|done|ready|in place))'
    r'|(?:all (?:changes|tasks|work) (?:have been|are) (?:completed|done|applied))'
    r'|(?:(?:task|work) (?:is )?(?:complete|done|finished))'
    r'|(?:successfully (?:implemented|completed|updated|created|fixed|written))'
    r')',
    re.IGNORECASE
)


def _looks_like_stall(response_text: str) -> bool:
    """Detect if the agent is stalling rather than delivering a final answer."""
    if _DONE_PATTERNS.search(response_text):
        return False
    if _STALL_PATTERNS.search(response_text):
        return True
    return False


def _check_history_budget(history, model="implementer", agent_theme=None):
    """Proactively manage history size with tiered compaction.

    Tier 1 (120K chars): Model-generated conversation summary.
    Tier 2 (150K chars): Hard trim as last resort (drops oldest 25%).

    Cheap fast path: measure raw character size first. _compress_history
    truncates large messages but is a full O(n) pass over history; running
    it on every turn just to size-check costs measurable wall time on
    long sessions. Only run the full compress when raw size is in the
    danger band.
    """
    from qwen_client.completion import _trim_history_for_context, _compress_history
    from qwen_client.compaction import compact_conversation

    # Cheap raw-size estimate; well below the 120K threshold short-circuit.
    raw_chars = sum(len(m.get("content") or "") for m in history)
    if raw_chars < 100000:
        return

    # In the danger band — pay for the compressed view (same form sent
    # to the model).
    compressed = _compress_history(history)
    total_chars = sum(len(m.get("content", "")) for m in compressed)

    # Tier 1: model-generated summary (before the hard trim threshold)
    if total_chars > 120000:
        print_colored(
            f"\n[Client] Context at {total_chars // 1000}K chars. Running auto-compaction...",
            COLORS['WARNING']
        )
        success, msg = compact_conversation(history, model, agent_theme, reason="auto")
        if success:
            compressed = _compress_history(history)
            total_chars = sum(len(m.get("content", "")) for m in compressed)
            print_colored(f"  {msg} Now at {total_chars // 1000}K chars.", COLORS['GREEN'])
            return

    # Tier 2: hard trim as last resort
    if total_chars > HISTORY_CHAR_BUDGET:
        print_colored(
            f"\n[Client] History size ({total_chars // 1000}K chars) exceeds budget. Trimming...",
            COLORS['WARNING']
        )
        _trim_history_for_context(history)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process_agent_tasks(tasks, history, initial_model, agent_theme):
    """Execute a list of agent tasks sequentially."""
    model = initial_model
    task_idx = 0

    try:
        for task_idx, (task_agent, task_content) in enumerate(tasks):
            if not task_content.strip():
                continue

            task_theme = AGENT_THEMES[task_agent]
            print_colored(f"\n>>> Executing task with @{task_agent} {task_theme['icon']}", COLORS['BLUE'])
            set_terminal_title(f"Qwen - @{task_agent} Working...")

            if task_agent != model:
                model = task_agent
                agent_theme = AGENT_THEMES[model]

            # ── Reset per-task state ──
            reset_write_counts()
            agentic_ctx = AgenticContext(task_content)
            print_colored(
                f"  Query type: {agentic_ctx.query_type.value} "
                f"(budget: {agentic_ctx.budget.max_iterations} iterations)",
                COLORS['CYAN']
            )

            history.append({"role": "user", "content": task_content})
            save_chat_history(history, model)

            task_aborted = False
            task_commands_executed = False
            nudge_count = 0
            max_continuations = 5
            turn_count = 0
            consecutive_errors = 0
            recent_response_hashes = []  # Response-level loop detection
            MAX_TURNS_PER_TASK = 50
            MAX_CONSECUTIVE_ERRORS = 3
            MAX_IDENTICAL_RESPONSES = 3

            while True:
                # ── Safety cap: absolute turn limit ──
                turn_count += 1
                if turn_count > MAX_TURNS_PER_TASK:
                    print_colored(
                        f"\n[Safety] Task reached {MAX_TURNS_PER_TASK} turns. Forcing completion.",
                        COLORS['FAIL']
                    )
                    history.append({
                        "role": "user",
                        "content": "TURN LIMIT REACHED. Provide your final answer now based on everything gathered so far.",
                    })
                    save_chat_history(history, model)
                    synth_text, _, _ = get_completion(history, model, agent_theme)
                    if synth_text:
                        history.append({"role": "assistant", "content": synth_text})
                        save_chat_history(history, model)
                    break

                _check_history_budget(history, model, agent_theme)

                # ── Inject agentic context before completion ──
                injection = agentic_ctx.get_pre_completion_injection()

                native_tools = _native_tools_for(model)
                response_text, finish_reason, tool_calls = get_completion(
                    history, model, agent_theme, agentic_context=injection,
                    tools=native_tools,
                    tool_choice="auto" if native_tools else None,
                )
                if response_text is None:
                    consecutive_errors += 1
                    if consecutive_errors < MAX_CONSECUTIVE_ERRORS:
                        # Recovery: full compaction → abort
                        print_colored("\n[Recovery] Completion failed. Trying full compaction...", COLORS['WARNING'])
                        compact_conversation(history, model, agent_theme, reason="error_recovery")
                        continue
                    print_colored(f"\n[Recovery] {MAX_CONSECUTIVE_ERRORS} consecutive failures. Aborting task.", COLORS['FAIL'])
                    task_aborted = True
                    break
                consecutive_errors = 0  # reset on success

                # ── Handle interrupted responses (Ctrl+C) ──
                if finish_reason == "interrupted":
                    history.append({"role": "assistant", "content": response_text})
                    save_chat_history(history, model)
                    break

                # ── Handle truncated responses ──
                continuation_count = 0
                aggregated_response = response_text
                while finish_reason == "length" and continuation_count < max_continuations:
                    continuation_count += 1
                    print_colored(
                        f"\n[Continuation {continuation_count}/{max_continuations}] "
                        "Response was truncated. Requesting continuation...",
                        COLORS['WARNING']
                    )
                    history.append({"role": "assistant", "content": response_text})
                    history.append({
                        "role": "user",
                        "content": "Your previous response was cut off. Continue exactly where you left off.",
                        "auto_send": True,
                    })
                    save_chat_history(history, model)

                    cont_text, finish_reason, _ = get_completion(history, model, agent_theme)
                    if cont_text is None:
                        task_aborted = True
                        break
                    response_text = cont_text

                    if re.search(r'<{1,3}\w*$', aggregated_response):
                        cont_text = cont_text.lstrip()
                    aggregated_response += cont_text

                if task_aborted:
                    # Clean up fragmented history from failed continuations
                    if continuation_count > 0:
                        del history[-(continuation_count * 2):]
                        history.append({"role": "assistant", "content": aggregated_response})
                        save_chat_history(history, model)
                    break

                if continuation_count > 0:
                    response_text = aggregated_response
                    # Replace fragmented history entries (partial + continuation prompts)
                    # with the single coherent aggregated response.
                    del history[-(continuation_count * 2):]
                    history.append({"role": "assistant", "content": aggregated_response})
                    save_chat_history(history, model)
                else:
                    assistant_msg = {"role": "assistant", "content": response_text}
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    history.append(assistant_msg)
                    save_chat_history(history, model)

                # ── Native tool_calls dispatch ──
                # When the model emitted OpenAI-shape tool_calls (only enabled
                # when QWEN_NATIVE_TOOLS=1 + agent in NATIVE_TOOLS_AGENTS),
                # dispatch them and feed results back as role:"tool" messages.
                # Skip marker parsing entirely for this turn.
                if tool_calls:
                    print_colored(
                        f"\n[native tools] {len(tool_calls)} call(s) — dispatching... "
                        f"[{agentic_ctx.budget.current}/{agentic_ctx.budget.max_iterations}]",
                        COLORS['CYAN']
                    )
                    results = _dispatch_native_tool_calls(tool_calls, agentic_ctx)
                    for tc_id, name, output in results:
                        history.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": name,
                            "content": output if isinstance(output, str) else str(output),
                        })
                    save_chat_history(history, model)
                    task_commands_executed = True
                    nudge_count = 0
                    continue

                # ── Response-level loop detection ──
                # Catches loops where the model generates the same response repeatedly,
                # including when it bypasses WRITE_FILE loop detection via REMOTE_EXEC.
                resp_hash = hash(response_text.strip())
                recent_response_hashes.append(resp_hash)
                if len(recent_response_hashes) > MAX_IDENTICAL_RESPONSES + 2:
                    recent_response_hashes = recent_response_hashes[-(MAX_IDENTICAL_RESPONSES + 2):]
                identical_count = recent_response_hashes.count(resp_hash)
                if identical_count >= MAX_IDENTICAL_RESPONSES:
                    print_colored(
                        f"\n[Loop detected] Agent generated the same response {identical_count} times. "
                        "Breaking loop and forcing synthesis.",
                        COLORS['FAIL']
                    )
                    history.append({
                        "role": "user",
                        "content": (
                            "LOOP DETECTED: You have generated the same response multiple times. "
                            "This approach is not working. STOP retrying the same action. "
                            "Summarize what you accomplished so far and what is blocking you."
                        ),
                    })
                    save_chat_history(history, model)
                    synth_text, _, _ = get_completion(history, model, agent_theme)
                    if synth_text:
                        history.append({"role": "assistant", "content": synth_text})
                        save_chat_history(history, model)
                    break

                # ── Process agentic markers (strip before tool parsing) ──
                cleaned_response = agentic_ctx.process_response(response_text)

                # Display plan if updated
                plan_display = agentic_ctx.plan.display()
                if plan_display:
                    print_colored(plan_display, COLORS['CYAN'])

                # Display confidence if reported
                if agentic_ctx.confidence.current_confidence is not None:
                    print_colored(
                        f"  [Confidence: {agentic_ctx.confidence.current_confidence}%]",
                        COLORS['BLUE']
                    )

                # ── Execute commands found in the cleaned response ──
                tool_output = _process_remote_commands(cleaned_response)

                if not tool_output and finish_reason == "stop" and not _DONE_PATTERNS.search(cleaned_response):
                    fallback_cmds = _extract_fallback_commands(cleaned_response)
                    if fallback_cmds:
                        print_colored(
                            "\nAgent used code blocks instead of markers. Extracting commands...",
                            COLORS['WARNING']
                        )
                        results = []
                        total_len = 0
                        global_max_len = 40000

                        for i, cmd in enumerate(fallback_cmds):
                            if total_len > global_max_len:
                                results.append(
                                    f"\n... [OMITTED {len(fallback_cmds) - i} FALLBACK COMMANDS "
                                    "TO PREVENT CONTEXT OVERFLOW] ..."
                                )
                                break
                            result = _execute_remote_command(cmd.strip())
                            if result:
                                res_str = f"[Command {i+1}] {result}"
                                results.append(res_str)
                                total_len += len(res_str)

                        if results:
                            tool_output = "\n\n".join(results)

                if tool_output:
                    agentic_ctx.budget.increment()

                    # ── Budget exhaustion ──
                    if agentic_ctx.should_force_synthesis():
                        if agentic_ctx.query_type.value == "IMPLEMENT":
                            # Implementation tasks: warn but keep going — each iteration
                            # is productive work, not speculative retrieval.
                            # MAX_TURNS_PER_TASK is the real safety cap.
                            print_colored(
                                f"\n[Budget soft limit: {agentic_ctx.budget.current}/"
                                f"{agentic_ctx.budget.max_iterations} iterations — continuing implementation]",
                                COLORS['WARNING']
                            )
                        else:
                            # Retrieval/explain tasks: force synthesis
                            print_colored(
                                f"\n[Budget exhausted: {agentic_ctx.budget.current}/"
                                f"{agentic_ctx.budget.max_iterations} iterations]",
                                COLORS['WARNING']
                            )
                            history.append({
                                "role": "user",
                                "content": (
                                    f"Tool output:\n{tool_output}\n\n"
                                    "RETRIEVAL BUDGET EXHAUSTED. Synthesize your final answer "
                                    "now from all information gathered."
                                ),
                            })
                            save_chat_history(history, model)
                            synth_text, _, _ = get_completion(history, model, agent_theme)
                            if synth_text:
                                history.append({"role": "assistant", "content": synth_text})
                                save_chat_history(history, model)
                            break

                    task_commands_executed = True
                    nudge_count = 0
                    cmd_count = tool_output.count("[Command ")
                    label = f"{cmd_count} command(s) executed" if cmd_count > 1 else "Tool result"
                    print_colored(
                        f"\n{label}. Sending output back to agent... "
                        f"[{agentic_ctx.budget.current}/{agentic_ctx.budget.max_iterations}]",
                        COLORS['CYAN']
                    )
                    history.append({"role": "user", "content": f"Tool output:\n{tool_output}"})
                    save_chat_history(history, model)
                    continue

                # ── Stall detection ──
                # Nudge if: (a) agent previously executed tools but now stalled, OR
                # (b) first few turns and agent is planning without acting.
                _is_stalling = _looks_like_stall(cleaned_response)
                if ((task_commands_executed or turn_count <= 2)
                        and nudge_count < MAX_STALL_NUDGES
                        and _is_stalling):
                    nudge_count += 1
                    print_colored(
                        f"\n[Nudge {nudge_count}/{MAX_STALL_NUDGES}] "
                        "Agent produced a summary instead of acting. Nudging to continue...",
                        COLORS['WARNING']
                    )
                    history.append({
                        "role": "user",
                        "content": (
                            "You described what needs to be done but didn't execute any commands. "
                            "Stop summarizing and proceed with the implementation now. "
                            "Use your tools (<<<WRITE_FILE>>>, <<<EDIT_FILE>>>, <<<REMOTE_EXEC>>>, etc.) "
                            "to make the actual changes."
                        ),
                    })
                    save_chat_history(history, model)
                    continue

                # ── Thinking turn: response was only agentic markers with pending plan ──
                has_pending_steps = (
                    not agentic_ctx.plan.is_empty
                    and any(not s["done"] for s in agentic_ctx.plan.steps)
                )
                # Also treat a non-empty plan with a goal as "pending" even if the
                # model omitted the STEPS section (abbreviated plan update).
                has_active_plan = (
                    not agentic_ctx.plan.is_empty
                    and agentic_ctx.plan.goal is not None
                )
                # Strip residual XML-like tags (any case, any bracket count) that aren't
                # tool commands or real prose — catches degenerate output like a lone
                # "<continue>" or "<<<CONTINUE>>>" (the documented continuation marker that
                # otherwise has no handler).  Without this, such responses would slip through
                # as "substantive" and the orchestrator would prematurely declare the task
                # complete instead of nudging with the next plan step.
                _substantive = re.sub(r'<+/?[A-Za-z_]\w*>+\s*\d*', '', cleaned_response).strip()
                if not _substantive and (has_pending_steps or has_active_plan):
                    print_colored(
                        "\n[Thinking turn] Agent updated plan/scratchpad. Nudging to continue...",
                        COLORS['CYAN']
                    )
                    next_step = next(
                        (s["text"] for s in agentic_ctx.plan.steps if not s["done"]),
                        agentic_ctx.plan.goal or "the next step"
                    )
                    history.append({
                        "role": "user",
                        "content": (
                            f"Good — your plan and scratchpad are updated. Now proceed with: {next_step}\n"
                            "Use your tools to execute this step."
                        ),
                    })
                    save_chat_history(history, model)
                    continue

                # Agent is genuinely done
                if agentic_ctx.budget.current > 0:
                    print_colored(
                        f"  [Task complete after {agentic_ctx.budget.current} tool iterations, {turn_count} turns]",
                        COLORS['GREEN']
                    )
                break

            if task_aborted:
                remaining_tasks = tasks[task_idx + 1:]
                if remaining_tasks:
                    with _pending_tasks_lock:
                        PENDING_TASKS.extend(remaining_tasks)
                    print_colored(f"\n  Task aborted. Saved {len(remaining_tasks)} pending tasks.", COLORS['WARNING'])
                    print_colored("   Type '/resume' to retry/continue.", COLORS['BLUE'])
                break

    except KeyboardInterrupt:
        print_colored("\nInterrupt received.", COLORS['WARNING'])
        remaining_tasks = tasks[task_idx + 1:]
        if remaining_tasks:
            with _pending_tasks_lock:
                PENDING_TASKS.extend(remaining_tasks)
            remaining_agents = [t[0] for t in remaining_tasks]
            print_colored(f"  Skipped remaining tasks for: {', '.join(remaining_agents)}", COLORS['WARNING'])
            print_colored("   Type '/resume' to continue later.", COLORS['BLUE'])
    except Exception as e:
        print_colored(f"\nMain Loop Error: {e}", COLORS['FAIL'])

    set_terminal_title("Qwen - Idle")
    with _pending_tasks_lock:
        if not PENDING_TASKS:
            send_macos_notification("All tasks completed.", title="Qwen Client")

    return model
