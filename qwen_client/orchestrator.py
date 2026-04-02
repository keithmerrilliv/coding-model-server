"""Agent task orchestrator — the core tool-calling loop."""
import re
import threading

from qwen_client.config import COLORS, HISTORY_CHAR_BUDGET, print_colored
from qwen_client.display import set_terminal_title, send_macos_notification
from qwen_client.models import AGENT_THEMES
from qwen_client.history import save_chat_history
from qwen_client.completion import get_completion
from qwen_client.compaction import microcompact, compact_conversation
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

    Tier 1 (60K chars):  Microcompact old tool outputs (no model call).
    Tier 2 (120K chars): Model-generated conversation summary.
    Tier 3 (150K chars): Hard trim as last resort (drops oldest 25%).
    """
    from qwen_client.completion import _trim_history_for_context
    from qwen_client.compaction import microcompact, compact_conversation

    total_chars = sum(len(m.get("content", "")) for m in history)

    # Tier 1: cheap microcompaction of old tool outputs
    if total_chars > 60000:
        microcompact(history)
        total_chars = sum(len(m.get("content", "")) for m in history)

    # Tier 2: model-generated summary (before the hard trim threshold)
    if total_chars > 120000:
        print_colored(
            f"\n[Client] Context at {total_chars // 1000}K chars. Running auto-compaction...",
            COLORS['WARNING']
        )
        success, msg = compact_conversation(history, model, agent_theme, reason="auto")
        if success:
            total_chars = sum(len(m.get("content", "")) for m in history)
            print_colored(f"  {msg} Now at {total_chars // 1000}K chars.", COLORS['GREEN'])
            return

    # Tier 3: hard trim as last resort
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
                    synth_text, _ = get_completion(history, model, agent_theme)
                    if synth_text:
                        history.append({"role": "assistant", "content": synth_text})
                        save_chat_history(history, model)
                    break

                _check_history_budget(history, model, agent_theme)

                # ── Inject agentic context before completion ──
                injection = agentic_ctx.get_pre_completion_injection()

                response_text, finish_reason = get_completion(
                    history, model, agent_theme, agentic_context=injection
                )
                if response_text is None:
                    consecutive_errors += 1
                    if consecutive_errors < MAX_CONSECUTIVE_ERRORS:
                        # Staged recovery: microcompact → full compact → abort
                        if consecutive_errors == 1:
                            print_colored("\n[Recovery] Completion failed. Trying microcompaction...", COLORS['WARNING'])
                            microcompact(history)
                            continue
                        else:
                            print_colored("\n[Recovery] Still failing. Trying full compaction...", COLORS['WARNING'])
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

                    cont_text, finish_reason = get_completion(history, model, agent_theme)
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
                    history.append({"role": "assistant", "content": response_text})
                    save_chat_history(history, model)

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
                    synth_text, _ = get_completion(history, model, agent_theme)
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

                    # ── Budget exhaustion: force synthesis ──
                    if agentic_ctx.should_force_synthesis():
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
                        # One final completion for synthesis, then done
                        synth_text, _ = get_completion(history, model, agent_theme)
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
                if (task_commands_executed
                        and nudge_count < MAX_STALL_NUDGES
                        and _looks_like_stall(cleaned_response)):
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
                # Strip residual XML-like tags that aren't tool commands or real prose
                _substantive = re.sub(r'</?[A-Z_]+\w*>\s*\d*', '', cleaned_response).strip()
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
