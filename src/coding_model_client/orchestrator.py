"""Agent task orchestrator — the core tool-calling loop."""
import json
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum

from coding_model_client.config import COLORS, HISTORY_CHAR_BUDGET, print_colored
from coding_model_client.display import set_terminal_title, send_macos_notification
from coding_model_client.models import AGENT_THEMES
from coding_model_client.history import save_chat_history
from coding_model_client.completion import get_completion
from coding_model_client.compaction import compact_conversation
from coding_model_client.agentic.context import AgenticContext
from coding_model_server.tool_handlers import reset_write_counts

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
# When CODING_MODEL_NATIVE_TOOLS=1 and the active model is in NATIVE_TOOLS_AGENTS,
# we send an OpenAI `tools` array with the request and dispatch any returned
# `tool_calls` instead of relying on <<<TAG>>> marker parsing. Only
# REMOTE_EXEC has been migrated; every other tool still flows through markers.
NATIVE_TOOLS_AGENTS = {"native_implementer"}

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

    Gated on CODING_MODEL_NATIVE_TOOLS=1 and the agent being in NATIVE_TOOLS_AGENTS.
    """
    if os.environ.get("CODING_MODEL_NATIVE_TOOLS") != "1":
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


# The budget guidance (Config.TOKEN_BUDGET_GUIDANCE §3) tells the model it may
# stop early on a task too large for one response by ending with <<<CONTINUE>>>
# and a REMAINING list, promising "the client will automatically request
# continuation". The client only ever honoured a *hard* cut-off — finish_reason
# == "length" — so a VOLUNTARY stop came back as finish_reason "stop", fell
# through the continuation loop, and the marker was stripped as degenerate
# output with the REMAINING items dropped on the floor. The model had taken an
# offer nothing implemented.
#
# Tolerate the sloppy variants the model actually emits (<continue>, ***, case,
# stray colon) rather than only the canonical form, and anchor to the END of the
# response so a mid-answer mention of the marker (e.g. the model quoting these
# very instructions) doesn't trigger a continuation.
_CONTINUE_SIGNAL_RE = re.compile(
    r'\**\s*<{1,3}\s*CONTINUE\s*>{0,3}\s*:?\s*\**\s*'
    r'(?:\n+\**\s*REMAINING\s*\**\s*:?(?P<remaining>.*?))?\s*\Z',
    re.IGNORECASE | re.DOTALL,
)


def _split_continue_signal(text):
    """Split a trailing <<<CONTINUE>>> signal off a response.

    Returns (text_without_signal, remaining_note). ``remaining_note`` is None
    when the model did not signal continuation, "" when it signalled without
    naming what's left, and otherwise the REMAINING list it gave us — which we
    feed back so it picks up the right thread.
    """
    if not text:
        return text, None
    match = _CONTINUE_SIGNAL_RE.search(text)
    if not match:
        return text, None
    remaining = (match.group("remaining") or "").strip()
    return text[:match.start()].rstrip(), remaining


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
    from coding_model_client.completion import trim_history_in_place, _compress_history
    from coding_model_client.compaction import compact_conversation

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
        trim_history_in_place(history)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# ── One turn of the agent loop ───────────────────────────────────────────────
#
# process_agent_tasks used to be one 430-line function nested nine deep: the
# per-task loop body handled completion, continuation, native tool dispatch,
# loop detection, marker processing, command execution, budget and stall
# nudges, and signalled its decisions with break/continue against locals no
# test could reach. That body is now _run_one_turn, which takes the mutable
# per-task state explicitly and RETURNS what the loop should do next
# (DEV-152). Behaviour is unchanged — each former `continue` is CONTINUE,
# each `break` is DONE, and the two abort paths are ABORTED.

MAX_TURNS_PER_TASK = 50
MAX_CONSECUTIVE_ERRORS = 3
MAX_IDENTICAL_RESPONSES = 3
MAX_CONTINUATIONS = 5


class TurnOutcome(Enum):
    """What the task loop should do after a turn."""

    CONTINUE = "continue"   # feed the result back and take another turn
    DONE = "done"           # task finished — move to the next task
    ABORTED = "aborted"     # unrecoverable — stop and save the remaining tasks


@dataclass
class TurnState:
    """Mutable per-task state threaded through the turns of one task.

    Everything the old loop kept in locals. `history` is mutated in place
    (turns append to it, and the continuation path deletes fragments off the
    end), so it is the caller's list, not a copy.
    """

    history: list
    model: str
    agent_theme: dict
    agentic_ctx: object
    turn_count: int = 0
    consecutive_errors: int = 0
    nudge_count: int = 0
    task_commands_executed: bool = False
    recent_response_hashes: list = field(default_factory=list)


def _run_one_turn(state: TurnState) -> TurnOutcome:
    """Run a single turn of the agent loop and report what to do next."""
    # ── Safety cap: absolute turn limit ──
    state.turn_count += 1
    if state.turn_count > MAX_TURNS_PER_TASK:
        print_colored(
            f"\n[Safety] Task reached {MAX_TURNS_PER_TASK} turns. Forcing completion.",
            COLORS['FAIL']
        )
        state.history.append({
            "role": "user",
            "content": "TURN LIMIT REACHED. Provide your final answer now based on everything gathered so far.",
        })
        save_chat_history(state.history, state.model)
        synth_text, _, _ = get_completion(state.history, state.model, state.agent_theme)
        if synth_text:
            state.history.append({"role": "assistant", "content": synth_text})
            save_chat_history(state.history, state.model)
        return TurnOutcome.DONE

    _check_history_budget(state.history, state.model, state.agent_theme)

    # ── Inject agentic context before completion ──
    injection = state.agentic_ctx.get_pre_completion_injection()

    native_tools = _native_tools_for(state.model)
    response_text, finish_reason, tool_calls = get_completion(
        state.history, state.model, state.agent_theme, agentic_context=injection,
        tools=native_tools,
        tool_choice="auto" if native_tools else None,
    )
    if response_text is None:
        state.consecutive_errors += 1
        if state.consecutive_errors < MAX_CONSECUTIVE_ERRORS:
            # Recovery: full compaction → abort
            print_colored("\n[Recovery] Completion failed. Trying full compaction...", COLORS['WARNING'])
            compact_conversation(state.history, state.model, state.agent_theme, reason="error_recovery")
            return TurnOutcome.CONTINUE
        print_colored(f"\n[Recovery] {MAX_CONSECUTIVE_ERRORS} consecutive failures. Aborting task.", COLORS['FAIL'])
        return TurnOutcome.ABORTED
    state.consecutive_errors = 0  # reset on success

    # ── Handle interrupted responses (Ctrl+C) ──
    if finish_reason == "interrupted":
        state.history.append({"role": "assistant", "content": response_text})
        save_chat_history(state.history, state.model)
        return TurnOutcome.DONE

    # ── Handle truncated or voluntarily-deferred responses ──
    # Two ways a response can be incomplete: the state.model was CUT OFF
    # (finish_reason "length"), or it chose to stop and said so with
    # <<<CONTINUE>>> (finish_reason "stop"). Both mean "there's more
    # to come" and both are answered by the same continuation turn.
    continuation_count = 0
    aborted = False
    response_text, remaining_note = _split_continue_signal(response_text)
    aggregated_response = response_text
    while (
        (finish_reason == "length" or remaining_note is not None)
        and continuation_count < MAX_CONTINUATIONS
    ):
        continuation_count += 1
        voluntary = remaining_note is not None
        if voluntary:
            reason = "Model signalled more work remains"
            nudge = (
                "You ended with <<<CONTINUE>>>. Pick up exactly where you "
                "left off and finish the remaining work. Do not repeat what "
                "you already produced."
            )
            if remaining_note:
                nudge += f"\n\nStill to do, per your own list:\n{remaining_note}"
        else:
            reason = "Response was truncated"
            nudge = (
                "Your previous response was cut off. Continue exactly "
                "where you left off."
            )
        print_colored(
            f"\n[Continuation {continuation_count}/{MAX_CONTINUATIONS}] "
            f"{reason}. Requesting continuation...",
            COLORS['WARNING']
        )
        state.history.append({"role": "assistant", "content": response_text})
        state.history.append({
            "role": "user",
            "content": nudge,
            "auto_send": True,
        })
        save_chat_history(state.history, state.model)

        cont_text, finish_reason, _ = get_completion(state.history, state.model, state.agent_theme)
        if cont_text is None:
            aborted = True
            break
        cont_text, remaining_note = _split_continue_signal(cont_text)
        response_text = cont_text

        if voluntary:
            # The state.model stopped at a clean boundary, so the two halves
            # are separate blocks — don't fuse the last line of one to
            # the first line of the next.
            if aggregated_response and not aggregated_response.endswith("\n"):
                aggregated_response += "\n"
            aggregated_response += cont_text.lstrip()
        else:
            # Cut off mid-token: splice the halves back together as-is.
            if re.search(r'<{1,3}\w*$', aggregated_response):
                cont_text = cont_text.lstrip()
            aggregated_response += cont_text

    if aborted:
        # Clean up fragmented state.history from failed continuations
        if continuation_count > 0:
            del state.history[-(continuation_count * 2):]
            state.history.append({"role": "assistant", "content": aggregated_response})
            save_chat_history(state.history, state.model)
        return TurnOutcome.ABORTED

    if remaining_note is not None:
        # Ran out of continuation budget while the state.model still says it
        # has work left. Say so — the aggregated answer looks finished
        # otherwise, and the leftovers would vanish silently.
        print_colored(
            f"\n[Continuation] Model still reports unfinished work after "
            f"{MAX_CONTINUATIONS} continuations; delivering what it has."
            + (f" Left undone: {remaining_note}" if remaining_note else ""),
            COLORS['WARNING']
        )

    if continuation_count > 0:
        response_text = aggregated_response
        # Replace fragmented state.history entries (partial + continuation prompts)
        # with the single coherent aggregated response.
        del state.history[-(continuation_count * 2):]
        state.history.append({"role": "assistant", "content": aggregated_response})
        save_chat_history(state.history, state.model)
    else:
        assistant_msg = {"role": "assistant", "content": response_text}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        state.history.append(assistant_msg)
        save_chat_history(state.history, state.model)

    # ── Native tool_calls dispatch ──
    # When the state.model emitted OpenAI-shape tool_calls (only enabled
    # when CODING_MODEL_NATIVE_TOOLS=1 + agent in NATIVE_TOOLS_AGENTS),
    # dispatch them and feed results back as role:"tool" messages.
    # Skip marker parsing entirely for this turn.
    if tool_calls:
        print_colored(
            f"\n[native tools] {len(tool_calls)} call(s) — dispatching... "
            f"[{state.agentic_ctx.budget.current}/{state.agentic_ctx.budget.max_iterations}]",
            COLORS['CYAN']
        )
        results = _dispatch_native_tool_calls(tool_calls, state.agentic_ctx)
        for tc_id, name, output in results:
            state.history.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": output if isinstance(output, str) else str(output),
            })
        save_chat_history(state.history, state.model)
        state.task_commands_executed = True
        state.nudge_count = 0
        return TurnOutcome.CONTINUE

    # ── Response-level loop detection ──
    # Catches loops where the state.model generates the same response repeatedly,
    # including when it bypasses WRITE_FILE loop detection via REMOTE_EXEC.
    resp_hash = hash(response_text.strip())
    state.recent_response_hashes.append(resp_hash)
    if len(state.recent_response_hashes) > MAX_IDENTICAL_RESPONSES + 2:
        state.recent_response_hashes = state.recent_response_hashes[-(MAX_IDENTICAL_RESPONSES + 2):]
    identical_count = state.recent_response_hashes.count(resp_hash)
    if identical_count >= MAX_IDENTICAL_RESPONSES:
        print_colored(
            f"\n[Loop detected] Agent generated the same response {identical_count} times. "
            "Breaking loop and forcing synthesis.",
            COLORS['FAIL']
        )
        state.history.append({
            "role": "user",
            "content": (
                "LOOP DETECTED: You have generated the same response multiple times. "
                "This approach is not working. STOP retrying the same action. "
                "Summarize what you accomplished so far and what is blocking you."
            ),
        })
        save_chat_history(state.history, state.model)
        synth_text, _, _ = get_completion(state.history, state.model, state.agent_theme)
        if synth_text:
            state.history.append({"role": "assistant", "content": synth_text})
            save_chat_history(state.history, state.model)
        return TurnOutcome.DONE

    # ── Process agentic markers (strip before tool parsing) ──
    cleaned_response = state.agentic_ctx.process_response(response_text)

    # Display plan if updated
    plan_display = state.agentic_ctx.plan.display()
    if plan_display:
        print_colored(plan_display, COLORS['CYAN'])

    # Display confidence if reported
    if state.agentic_ctx.confidence.current_confidence is not None:
        print_colored(
            f"  [Confidence: {state.agentic_ctx.confidence.current_confidence}%]",
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
            # Fallback commands are a heuristic guess at intent, not
            # an explicit tool call — confirm before running them,
            # in EVERY permission mode (DEV-136). Headless callers
            # (EOFError) skip rather than execute.
            for cmd in fallback_cmds:
                print_colored(f"    $ {cmd}", COLORS['CYAN'])
            try:
                _ok = input(
                    f"{COLORS['BOLD']}Run {len(fallback_cmds)} "
                    f"extracted command(s)? [y/N] > {COLORS['ENDC']}"
                )
            except (EOFError, KeyboardInterrupt):
                _ok = ""
            if _ok.lower() != "y":
                print_colored("    Skipped extracted commands.",
                              COLORS['WARNING'])
                fallback_cmds = []
        if fallback_cmds:
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
        state.agentic_ctx.budget.increment()

        # ── Budget exhaustion ──
        if state.agentic_ctx.should_force_synthesis():
            if state.agentic_ctx.query_type.value == "IMPLEMENT":
                # Implementation tasks: warn but keep going — each iteration
                # is productive work, not speculative retrieval.
                # MAX_TURNS_PER_TASK is the real safety cap.
                print_colored(
                    f"\n[Budget soft limit: {state.agentic_ctx.budget.current}/"
                    f"{state.agentic_ctx.budget.max_iterations} iterations — continuing implementation]",
                    COLORS['WARNING']
                )
            else:
                # Retrieval/explain tasks: force synthesis
                print_colored(
                    f"\n[Budget exhausted: {state.agentic_ctx.budget.current}/"
                    f"{state.agentic_ctx.budget.max_iterations} iterations]",
                    COLORS['WARNING']
                )
                state.history.append({
                    "role": "user",
                    "content": (
                        f"Tool output:\n{tool_output}\n\n"
                        "RETRIEVAL BUDGET EXHAUSTED. Synthesize your final answer "
                        "now from all information gathered."
                    ),
                })
                save_chat_history(state.history, state.model)
                synth_text, _, _ = get_completion(state.history, state.model, state.agent_theme)
                if synth_text:
                    state.history.append({"role": "assistant", "content": synth_text})
                    save_chat_history(state.history, state.model)
                return TurnOutcome.DONE

        state.task_commands_executed = True
        state.nudge_count = 0
        cmd_count = tool_output.count("[Command ")
        label = f"{cmd_count} command(s) executed" if cmd_count > 1 else "Tool result"
        print_colored(
            f"\n{label}. Sending output back to agent... "
            f"[{state.agentic_ctx.budget.current}/{state.agentic_ctx.budget.max_iterations}]",
            COLORS['CYAN']
        )
        state.history.append({"role": "user", "content": f"Tool output:\n{tool_output}"})
        save_chat_history(state.history, state.model)
        return TurnOutcome.CONTINUE

    # ── Stall detection ──
    # Nudge if: (a) agent previously executed tools but now stalled, OR
    # (b) first few turns and agent is planning without acting.
    _is_stalling = _looks_like_stall(cleaned_response)
    if ((state.task_commands_executed or state.turn_count <= 2)
            and state.nudge_count < MAX_STALL_NUDGES
            and _is_stalling):
        state.nudge_count += 1
        print_colored(
            f"\n[Nudge {state.nudge_count}/{MAX_STALL_NUDGES}] "
            "Agent produced a summary instead of acting. Nudging to continue...",
            COLORS['WARNING']
        )
        state.history.append({
            "role": "user",
            "content": (
                "You described what needs to be done but didn't execute any commands. "
                "Stop summarizing and proceed with the implementation now. "
                "Use your tools (<<<WRITE_FILE>>>, <<<EDIT_FILE>>>, <<<REMOTE_EXEC>>>, etc.) "
                "to make the actual changes."
            ),
        })
        save_chat_history(state.history, state.model)
        return TurnOutcome.CONTINUE

    # ── Thinking turn: response was only agentic markers with pending plan ──
    has_pending_steps = (
        not state.agentic_ctx.plan.is_empty
        and any(not s["done"] for s in state.agentic_ctx.plan.steps)
    )
    # Also treat a non-empty plan with a goal as "pending" even if the
    # state.model omitted the STEPS section (abbreviated plan update).
    has_active_plan = (
        not state.agentic_ctx.plan.is_empty
        and state.agentic_ctx.plan.goal is not None
    )
    # Strip residual XML-like tags (any case, any bracket count) that aren't
    # tool commands or real prose. Without this, such responses would slip
    # through as "substantive" and the orchestrator would prematurely declare
    # the task complete instead of nudging with the next plan step.
    # A *trailing* <<<CONTINUE>>> no longer reaches here — the continuation
    # loop consumes it (DEV-80). This still catches the leftovers: a marker
    # the state.model buried mid-response, or one that survived because the
    # continuation budget ran out.
    _substantive = re.sub(r'<+/?[A-Za-z_]\w*>+\s*\d*', '', cleaned_response).strip()
    if not _substantive and (has_pending_steps or has_active_plan):
        print_colored(
            "\n[Thinking turn] Agent updated plan/scratchpad. Nudging to continue...",
            COLORS['CYAN']
        )
        next_step = next(
            (s["text"] for s in state.agentic_ctx.plan.steps if not s["done"]),
            state.agentic_ctx.plan.goal or "the next step"
        )
        state.history.append({
            "role": "user",
            "content": (
                f"Good — your plan and scratchpad are updated. Now proceed with: {next_step}\n"
                "Use your tools to execute this step."
            ),
        })
        save_chat_history(state.history, state.model)
        return TurnOutcome.CONTINUE

    # Agent is genuinely done
    if state.agentic_ctx.budget.current > 0:
        print_colored(
            f"  [Task complete after {state.agentic_ctx.budget.current} tool iterations, {state.turn_count} turns]",
            COLORS['GREEN']
        )
    return TurnOutcome.DONE


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
            set_terminal_title(f"Coding Model - @{task_agent} Working...")

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

            state = TurnState(
                history=history,
                model=model,
                agent_theme=agent_theme,
                agentic_ctx=agentic_ctx,
            )
            while True:
                outcome = _run_one_turn(state)
                if outcome is not TurnOutcome.CONTINUE:
                    break
            task_aborted = outcome is TurnOutcome.ABORTED

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

    set_terminal_title("Coding Model - Idle")
    with _pending_tasks_lock:
        if not PENDING_TASKS:
            send_macos_notification("All tasks completed.", title="Coding Model Client")

    return model
