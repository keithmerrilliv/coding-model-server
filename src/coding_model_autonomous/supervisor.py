"""Supervisor agent — meta-orchestrator for the Phase 2 task DAG.

Replaces the hardcoded transition logic in ``orchestrator_daemon.py``
(``_attempt_retry``, ``_handle_gate_rejection``) with a single LLM call
that emits a structured decision via native tool-calling.

The supervisor sees only the *outcome* of a phase (success / test failure /
review rejection / agent error / stall), never raw transcripts. Output is
forced through a JSON schema (one tool, ``tool_choice=required``) so the
daemon never has to parse free-form text.

Budget is tracked separately from ``MAX_RETRIES``: the supervisor counts
*decisions per spec* so a runaway can't reset itself by bouncing roles.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Literal, Optional, TypedDict

import requests  # for requests.RequestException in callers below

from coding_model_autonomous._http import post_chat_completion

logger = logging.getLogger("orchestrator.supervisor")


# ── Configuration ────────────────────────────────────────────────────────────

SUPERVISOR_AGENT = os.getenv("AUTONOMOUS_SUPERVISOR_AGENT", "supervisor")
SUPERVISOR_TIMEOUT = float(os.getenv("AUTONOMOUS_SUPERVISOR_TIMEOUT", "600"))
SUPERVISOR_MAX_TOKENS = int(os.getenv("AUTONOMOUS_SUPERVISOR_MAX_TOKENS", "1500"))
MAX_SUPERVISOR_TRANSITIONS = int(
    os.getenv("AUTONOMOUS_MAX_SUPERVISOR_TRANSITIONS", "8")
)


# ── Types ────────────────────────────────────────────────────────────────────

Phase = Literal["plan", "architect", "implementer", "reviewer"]
Outcome = Literal[
    "success",
    "test_fail",
    "review_reject",
    "agent_error",
    "schema_violation",
    "stalled",
]
Action = Literal[
    "advance",
    "retry",
    "replan",
    "request_clarification",
    "abort",
]
Role = Literal["architect", "implementer", "reviewer"]


class ArtifactSummary(TypedDict, total=False):
    kind: str           # "design" | "code" | "test" | "review"
    path: str
    bytes: int


class PriorDecision(TypedDict, total=False):
    action: str
    target_role: Optional[str]
    reason: str


class SupervisorContext(TypedDict, total=False):
    spec_id: str
    spec_title: str
    phase: Phase
    role: Optional[Role]
    outcome: Outcome
    retry_count: int
    transitions_used: int
    plan_yaml: Optional[str]
    reviewer_notes: Optional[str]
    test_output_excerpt: Optional[str]
    agent_error_excerpt: Optional[str]
    artifacts: list[ArtifactSummary]
    prior_decisions: list[PriorDecision]  # oldest-first; supervisor's own history


@dataclass
class SupervisorDecision:
    action: Action
    target_role: Optional[Role]
    feedback_to_inject: Optional[str]
    reason: str
    raw_tool_call: dict


class SupervisorError(RuntimeError):
    """Raised when the supervisor call fails or returns invalid output.

    Daemon callers should treat this as a non-fatal signal to fall back
    to the legacy deterministic transition (don't crash the spec).
    """


# ── Decision schema (the only tool the supervisor may call) ──────────────────

DECIDE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "decide",
        "description": (
            "Emit the orchestration decision for the current phase outcome. "
            "Call exactly once. Do not produce any free-text response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "advance",
                        "retry",
                        "replan",
                        "request_clarification",
                        "abort",
                    ],
                    "description": (
                        "advance: phase succeeded or rejection was unwarranted, "
                        "mark the task DONE and continue. "
                        "retry: re-run target_role with feedback_to_inject as "
                        "rejection notes. "
                        "replan: send the spec back to the planner; the prior "
                        "plan is structurally wrong. "
                        "request_clarification: ask the human a question via a "
                        "CLARIFICATION gate; feedback_to_inject is the question. "
                        "abort: mark the spec FAILED; recovery is unlikely."
                    ),
                },
                "target_role": {
                    "type": "string",
                    "enum": ["architect", "implementer", "reviewer"],
                    "description": (
                        "Required when action=retry. The role whose task to "
                        "re-run. Omit for other actions."
                    ),
                },
                "feedback_to_inject": {
                    "type": "string",
                    "description": (
                        "Required for retry and request_clarification. For "
                        "retry: the rejection notes the next agent will see. "
                        "For request_clarification: the question for the human. "
                        "Be specific and reference file paths or test names."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence, logged to the events table and shown "
                        "to the human. State the decision and the principle "
                        "driving it."
                    ),
                },
            },
            "required": ["action", "reason"],
            "additionalProperties": False,
        },
    },
}


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the supervisor agent for an autonomous software-development pipeline. \
Worker agents (architect, implementer, reviewer) produce artifacts and review gates; \
your job is to decide what happens next when a phase finishes — successfully or not.

You will receive a structured snapshot of the current phase's outcome. You MUST \
respond by calling the `decide` tool exactly once. Do not write any free-text \
response. Do not call any other tool.

Decision policy:

1. SUCCESS path. If the phase succeeded and produced sound artifacts, choose \
`advance`. This is the default; do not retry working code.

2. RETRY vs REVISE-DESIGN vs REPLAN. Three escalation levels — pick the right one.
   - `retry` with target_role=implementer (or reviewer) is for *local* defects: \
a flaky test, a missing import, one wrong function, a misread spec line. The \
same role's next attempt can plausibly fix it.
   - `retry` with target_role=architect is for *design* defects. If the SAME \
test/assertion keeps failing across multiple implementer attempts — ESPECIALLY \
across different implementer models — the implementers are faithfully building a \
wrong or under-specified DESIGN; retrying the implementer again only yields more \
wrong code. Re-run the architect so it fixes the design (e.g. states the \
invariant the failing test encodes); the implementer then rebuilds against the \
corrected design. This is the surgical fix for "every model fails the same \
assertion," and you should reach for it BEFORE exhausting the implementer's \
retry budget on a recurring failure. Put the concrete failing behavior in \
`feedback_to_inject` so the architect knows what to fix.
   - `replan` is for *structural* defects above the design level: wrong \
framework, incoherent task decomposition. Heavier than retry→architect; prefer \
retry→architect when only the design's content is wrong.

3. CLARIFICATION. If progress is blocked on missing user intent that no agent \
can resolve (ambiguous spec, conflicting requirements, unspecified API target), \
choose `request_clarification`. Do not retry agents on questions only a human \
can answer.

4. RETRY BUDGET. Each task has a `retry_count` and a hard cap. If the role has \
already retried multiple times on the same kind of failure, escalate: `replan` \
if the design still looks salvageable, `abort` if it does not. Don't burn the \
final retry on the same failure mode.

5. PRIOR DECISIONS. The "Prior supervisor decisions for this spec" block lists \
what you already chose. If you previously picked `retry` (or any action) and \
the same outcome is recurring, that approach is not working — escalate \
(`replan`, `abort`, or `request_clarification`) instead of repeating yourself. \
Each repeated decision burns a transition without progress. A second `retry` \
on the same role with similar feedback is almost always wrong.

6. SUPERVISOR BUDGET. `transitions_used` counts your own decisions for this \
spec. As it approaches the cap, prefer terminal actions (`advance` if defensible, \
otherwise `abort`) over more retries.

7. REJECTION OVERRIDE. A reviewer can be wrong. If reviewer_notes flags style \
nits or speculative concerns while the artifact meets the spec's success \
criteria, you may choose `advance` — but say so explicitly in `reason`.

8. EVIDENCE. Your `reason` field is auditable. Reference the specific signal \
(test name, error class, file path, criterion) that drove the decision. Vague \
reasons mean you're guessing.

When you call `retry`, `feedback_to_inject` becomes the rejection notes the \
next agent reads. Be concrete: tell it what to change, not what was wrong in \
the abstract."""


# ── Context formatting ───────────────────────────────────────────────────────

def format_context_message(ctx: SupervisorContext) -> str:
    """Render the SupervisorContext as the user-message body the model sees.

    Compact and structured. No raw transcripts — only outcomes and excerpts
    that have already been trimmed upstream (test output via
    ``_extract_actionable_test_output``, reviewer notes from the gate).
    """
    lines = [
        f"## Spec: {ctx.get('spec_title', '(untitled)')} ({ctx['spec_id']})",
        f"## Phase: {ctx['phase']} (role={ctx.get('role') or '-'})",
        f"## Outcome: {ctx['outcome']}",
        f"## Retry count for this role: {ctx.get('retry_count', 0)}",
        f"## Supervisor transitions used: {ctx.get('transitions_used', 0)}"
        f" / {MAX_SUPERVISOR_TRANSITIONS}",
    ]

    prior = ctx.get("prior_decisions") or []
    if prior:
        lines.append("\n## Prior supervisor decisions for this spec (oldest first)")
        for i, d in enumerate(prior, 1):
            target = f" → {d.get('target_role')}" if d.get("target_role") else ""
            lines.append(f"{i}. {d.get('action', '?')}{target}: {d.get('reason', '').strip()}")

    artifacts = ctx.get("artifacts") or []
    if artifacts:
        lines.append("\n## Artifacts produced this phase")
        for a in artifacts:
            kind = a.get("kind", "?")
            path = a.get("path", "?")
            size = a.get("bytes")
            size_str = f" ({size} bytes)" if size is not None else ""
            lines.append(f"- {kind}: {path}{size_str}")

    notes = ctx.get("reviewer_notes")
    if notes:
        lines.append("\n## Reviewer notes")
        lines.append(notes.strip())

    test_excerpt = ctx.get("test_output_excerpt")
    if test_excerpt:
        lines.append("\n## Test output excerpt")
        lines.append("```")
        lines.append(test_excerpt.strip())
        lines.append("```")

    err = ctx.get("agent_error_excerpt")
    if err:
        lines.append("\n## Agent error")
        lines.append("```")
        lines.append(err.strip())
        lines.append("```")

    plan = ctx.get("plan_yaml")
    if plan:
        lines.append("\n## Current plan.yaml")
        lines.append("```yaml")
        lines.append(plan.strip())
        lines.append("```")

    lines.append(
        "\nDecide the next transition by calling the `decide` tool exactly once."
    )
    return "\n".join(lines)


# ── Inference call ───────────────────────────────────────────────────────────

def _post_completion(messages: list[dict]) -> dict:
    """POST to the coding-model-server inference API with forced tool-calling."""
    resp = post_chat_completion(
        SUPERVISOR_AGENT,
        messages,
        timeout=SUPERVISOR_TIMEOUT,
        max_tokens=SUPERVISOR_MAX_TOKENS,
        temperature=0.1,
        tools=[DECIDE_TOOL_SCHEMA],
        tool_choice={"type": "function", "function": {"name": "decide"}},
    )
    resp.raise_for_status()
    return resp.json()


def _parse_decision(data: dict) -> SupervisorDecision:
    """Extract and validate the supervisor's tool call.

    The model is forced to call ``decide`` via tool_choice; if it didn't,
    that's a server- or model-side bug worth surfacing as SupervisorError.
    """
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise SupervisorError(f"server response missing choices/message: {e}") from e

    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        raise SupervisorError(
            f"supervisor produced no tool_call (content={msg.get('content', '')[:200]!r})"
        )

    tc = tool_calls[0]
    fn = tc.get("function") or {}
    if fn.get("name") != "decide":
        raise SupervisorError(f"unexpected tool name: {fn.get('name')!r}")

    args_raw = fn.get("arguments") or "{}"
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except json.JSONDecodeError as e:
        raise SupervisorError(f"malformed tool arguments JSON: {e}") from e

    action = args.get("action")
    if action not in (
        "advance", "retry", "replan", "request_clarification", "abort"
    ):
        raise SupervisorError(f"invalid action: {action!r}")

    target_role = args.get("target_role")
    feedback = args.get("feedback_to_inject")
    reason = (args.get("reason") or "").strip()
    if not reason:
        raise SupervisorError("missing reason in decision")

    if action == "retry":
        if target_role not in ("architect", "implementer", "reviewer"):
            raise SupervisorError(
                f"retry requires target_role; got {target_role!r}"
            )
        if not feedback or not feedback.strip():
            raise SupervisorError("retry requires feedback_to_inject")
    elif action == "request_clarification":
        if not feedback or not feedback.strip():
            raise SupervisorError(
                "request_clarification requires feedback_to_inject (the question)"
            )
        target_role = None
    else:
        target_role = None
        if action != "retry":
            feedback = feedback if feedback and feedback.strip() else None

    return SupervisorDecision(
        action=action,
        target_role=target_role,
        feedback_to_inject=feedback,
        reason=reason,
        raw_tool_call=tc,
    )


# ── Public entry point ───────────────────────────────────────────────────────

def decide(ctx: SupervisorContext) -> SupervisorDecision:
    """Ask the supervisor agent to decide the next transition.

    Raises SupervisorError on transport/parse failure. Caller (the daemon)
    should catch and fall back to the legacy deterministic path.
    """
    if ctx.get("transitions_used", 0) >= MAX_SUPERVISOR_TRANSITIONS:
        logger.warning(
            "spec %s: supervisor budget exhausted (%d), forcing abort",
            ctx.get("spec_id"), ctx.get("transitions_used"),
        )
        return SupervisorDecision(
            action="abort",
            target_role=None,
            feedback_to_inject=None,
            reason=(
                f"Supervisor transition budget exhausted "
                f"({ctx.get('transitions_used')}/{MAX_SUPERVISOR_TRANSITIONS})"
            ),
            raw_tool_call={},
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": format_context_message(ctx)},
    ]

    logger.info(
        "supervisor.decide: spec=%s phase=%s outcome=%s retry=%d transitions=%d",
        ctx.get("spec_id"), ctx.get("phase"), ctx.get("outcome"),
        ctx.get("retry_count", 0), ctx.get("transitions_used", 0),
    )

    try:
        data = _post_completion(messages)
    except requests.RequestException as e:
        raise SupervisorError(f"supervisor inference call failed: {e}") from e

    decision = _parse_decision(data)
    logger.info(
        "supervisor decision: action=%s target=%s reason=%r",
        decision.action, decision.target_role, decision.reason,
    )
    return decision
