"""The client agent loop, one turn at a time (DEV-152).

process_agent_tasks was a single 430-line function nested nine deep, and its
control decisions lived in break/continue against locals. Reaching any one of
them from a test meant driving whole tasks end-to-end and inferring the
decision from side effects — so the safety machinery (turn cap, consecutive
-error abort, identical-response loop detection, stall nudges) was effectively
untested.

_run_one_turn takes the state and returns the decision, so each of those paths
is now one call and one assertion. test_continue_orchestration.py still covers
the same loop end-to-end; this covers the branches that were unreachable.
"""
import pytest

from coding_model_client import orchestrator
from coding_model_client.orchestrator import (
    MAX_CONSECUTIVE_ERRORS,
    MAX_IDENTICAL_RESPONSES,
    MAX_STALL_NUDGES,
    MAX_TURNS_PER_TASK,
    TurnOutcome,
    TurnState,
    _run_one_turn,
    set_tool_functions,
)
from coding_model_client.agentic.context import AgenticContext


@pytest.fixture
def turn(monkeypatch):
    """A TurnState wired to a scripted get_completion.

    Returns (state, script) where script(*replies) queues (text, finish_reason)
    tuples for successive completion calls.
    """
    monkeypatch.setattr(orchestrator, "save_chat_history", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "print_colored", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_check_history_budget", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "compact_conversation", lambda *a, **k: None)
    # No tools in play unless a test says otherwise: every response is prose.
    set_tool_functions(lambda _resp: "", lambda _resp: [], lambda _cmd: "")

    replies = []

    def script(*pairs):
        replies.extend(pairs)

    def fake_get_completion(history, model, agent_theme, **kwargs):
        if not replies:
            return "The task is complete.", "stop", None
        text, finish = replies.pop(0)
        return text, finish, None

    monkeypatch.setattr(orchestrator, "get_completion", fake_get_completion)

    state = TurnState(
        history=[{"role": "user", "content": "Build the thing."}],
        model="implementer",
        agent_theme={"icon": "*", "color": "", "name": "implementer"},
        agentic_ctx=AgenticContext("Build the thing."),
    )
    return state, script


def test_plain_answer_ends_the_task(turn):
    state, script = turn
    script(("Here is the answer. The task is complete.", "stop"))
    assert _run_one_turn(state) is TurnOutcome.DONE


def test_turn_cap_forces_synthesis_and_ends(turn):
    """The absolute safety cap: at the limit the loop stops asking for more
    work and demands a final answer instead of running forever."""
    state, script = turn
    state.turn_count = MAX_TURNS_PER_TASK
    script(("Final synthesized answer.", "stop"))

    assert _run_one_turn(state) is TurnOutcome.DONE
    prompts = [m["content"] for m in state.history if m["role"] == "user"]
    assert any("TURN LIMIT REACHED" in p for p in prompts)
    assert state.history[-1] == {"role": "assistant",
                                 "content": "Final synthesized answer."}


def test_completion_failure_retries_then_aborts(turn):
    """A failed completion is retried behind a compaction, but only so many
    times — the third consecutive failure aborts rather than looping."""
    state, script = turn
    script(*[(None, "stop")] * MAX_CONSECUTIVE_ERRORS)

    for _ in range(MAX_CONSECUTIVE_ERRORS - 1):
        assert _run_one_turn(state) is TurnOutcome.CONTINUE
    assert _run_one_turn(state) is TurnOutcome.ABORTED
    assert state.consecutive_errors == MAX_CONSECUTIVE_ERRORS


def test_a_success_resets_the_error_count(turn):
    state, script = turn
    script((None, "stop"), ("Done — the task is complete.", "stop"))
    assert _run_one_turn(state) is TurnOutcome.CONTINUE
    assert state.consecutive_errors == 1
    _run_one_turn(state)
    assert state.consecutive_errors == 0


def test_interrupt_ends_the_task_without_running_its_commands(turn):
    """Ctrl+C stops the turn where it stands. The partial answer is kept, but
    any command the model had emitted must NOT be executed on the way out —
    that is the whole point of interrupting."""
    state, script = turn
    executed = []
    set_tool_functions(lambda resp: executed.append(resp) or "[Command 1] ok",
                       lambda _resp: [], lambda _cmd: "")
    script(("Partial work <<<REMOTE_EXEC>>>rm -rf build<<<END>>>", "interrupted"))

    assert _run_one_turn(state) is TurnOutcome.DONE
    assert executed == [], "an interrupted turn ran its pending command"
    assert state.history[-1]["content"].startswith("Partial work")


def test_identical_responses_break_the_loop(turn):
    """Response-level loop detection: an agent re-running the same command with
    the same answer is stuck, so force a summary instead of letting it spin.

    Tool output on every turn is what makes this a loop rather than a finished
    task — without it the first turn would simply end.
    """
    state, script = turn
    set_tool_functions(lambda _resp: "[Command 1] ok", lambda _resp: [], lambda _cmd: "")
    stuck = "<<<REMOTE_EXEC>>>ls<<<END>>>"
    script(*[(stuck, "stop")] * MAX_IDENTICAL_RESPONSES)

    outcomes = [_run_one_turn(state) for _ in range(MAX_IDENTICAL_RESPONSES)]
    assert outcomes[:-1] == [TurnOutcome.CONTINUE] * (MAX_IDENTICAL_RESPONSES - 1)
    assert outcomes[-1] is TurnOutcome.DONE
    prompts = [m["content"] for m in state.history if m["role"] == "user"]
    assert any("LOOP DETECTED" in p for p in prompts)


def test_tool_output_feeds_back_and_spends_budget(turn, monkeypatch):
    state, script = turn
    set_tool_functions(lambda _resp: "[Command 1] ok", lambda _resp: [], lambda _cmd: "")
    script(("<<<REMOTE_EXEC>>>ls<<<END>>>", "stop"))
    before = state.agentic_ctx.budget.current

    assert _run_one_turn(state) is TurnOutcome.CONTINUE
    assert state.agentic_ctx.budget.current == before + 1
    assert state.task_commands_executed is True
    assert "Tool output:" in state.history[-1]["content"]


def test_stall_is_nudged_up_to_the_limit(turn):
    """An agent that narrates instead of acting gets nudged — but a bounded
    number of times, so a persistent narrator still terminates."""
    state, script = turn
    state.task_commands_executed = True
    # Distinct wording each turn: identical text would trip loop detection
    # first and prove nothing about the nudge limit.
    script(*[(f"Let me check the configuration file {i}.", "stop")
             for i in range(MAX_STALL_NUDGES + 1)])

    for i in range(MAX_STALL_NUDGES):
        assert _run_one_turn(state) is TurnOutcome.CONTINUE
        assert state.nudge_count == i + 1
    # Nudges exhausted: the same stall now ends the task instead of nudging.
    assert _run_one_turn(state) is TurnOutcome.DONE
    assert state.nudge_count == MAX_STALL_NUDGES
