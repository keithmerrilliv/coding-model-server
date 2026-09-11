"""Failure classification and disposition — one judgement per failed attempt (DEV-629).

Phase 2 of the pipeline-kernel refactor. The daemon used to decide at each
of roughly thirty ``except`` sites, by exception type, whether a failed
attempt was retried against the budget, bounced, or ended the spec. Nothing
asked the only question that matters: **was the model's output ever
evaluated?** A dead transport, a 413, a runner outage, a sandbox that could
not import the target package, an empty completion and a length-cut
response were each filed as if the code had been judged — runs 19, 20 and
21 died that way, and DEV-623 charged a truncation as unappliable edits.

``classify_*`` turn an exception, a model response or a test run into a
:class:`Failure` with one of three outcomes:

* **no-verdict** — nothing was evaluated. Requeue (or rotate to a different
  agent when the fault was agent-shaped: a 413, a truncation, an empty
  completion) with ``retry_count`` untouched. Never terminal. A run of
  consecutive no-verdicts on one attempt parks the task behind a gate that
  names the infrastructure, so an outage cannot spin forever either.
* **verdict** — real output was judged and found wanting: a parse failure,
  edits that did not apply, a build failure, red tests, a rejected review.
  Charged against ``MAX_RETRIES`` with the rotation, then synthesis.
* **terminal** — the spec cannot proceed: synthesis failed after the budget,
  the architect cannot produce an acceptable design, the operator aborted.
  The only branch that fails a spec, and it closes every task row with it
  (DEV-532).

``dispose`` is the only place that requeues, rotates, charges a retry, opens
a synthetic gate or fails a spec. Every disposition is one
``FAILURE_CLASSIFIED`` event, which is the taxonomy DEV-529 asked for.
The daemon supplies the two things this module must not import — the
synthesis escape hatch and the supervisor — as :class:`Hooks`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional

import requests

from .context import PromptTooLarge, RunnerOutage
from .models import EventKind, GateType, SpecStatus, TaskStatus

logger = logging.getLogger("orchestrator.outcome")


class Outcome(str, Enum):
    NO_VERDICT = "no_verdict"
    VERDICT = "verdict"
    TERMINAL = "terminal"


class FailureClass(str, Enum):
    # no-verdict
    TRANSPORT = "transport"                    # ConnectionError / Timeout
    HTTP_REFUSAL = "http_refusal"              # the model server answered 4xx/5xx
    SERVER_MALFORMED = "server_malformed"      # a 200 with no choices / content
    EMPTY_COMPLETION = "empty_completion"      # nothing visible after think-stripping
    TRUNCATED = "truncated"                    # finish_reason=length
    RUNNER_OUTAGE = "runner_outage"            # fetch outage / mac-runner unreachable
    PROMPT_TOO_LARGE = "prompt_too_large"      # no window holds the prompt (DEV-633)
    SANDBOX_PROVISIONING = "sandbox_provisioning"  # the repo's own package missing
    SHUTDOWN = "shutdown"                      # SIGTERM between calls
    UNKNOWN_EXCEPTION = "unknown_exception"    # a daemon bug, not a judgement
    # verdict
    PARSE_FAILURE = "parse_failure"
    UNAPPLIABLE_EDITS = "unappliable_edits"
    BUILD_FAILURE = "build_failure"
    TESTS_FAILED = "tests_failed"
    REVIEW_REJECTED = "review_rejected"
    # terminal
    SYNTHESIS_FAILED = "synthesis_failed"
    DESIGN_EXHAUSTED = "design_exhausted"
    ABORTED = "aborted"


NO_VERDICT_CLASSES = frozenset({
    FailureClass.TRANSPORT, FailureClass.HTTP_REFUSAL,
    FailureClass.SERVER_MALFORMED, FailureClass.EMPTY_COMPLETION,
    FailureClass.TRUNCATED, FailureClass.RUNNER_OUTAGE,
    FailureClass.PROMPT_TOO_LARGE,
    FailureClass.SANDBOX_PROVISIONING, FailureClass.SHUTDOWN,
    FailureClass.UNKNOWN_EXCEPTION,
})
VERDICT_CLASSES = frozenset({
    FailureClass.PARSE_FAILURE, FailureClass.UNAPPLIABLE_EDITS,
    FailureClass.BUILD_FAILURE, FailureClass.TESTS_FAILED,
    FailureClass.REVIEW_REJECTED,
})
TERMINAL_CLASSES = frozenset({
    FailureClass.SYNTHESIS_FAILED, FailureClass.DESIGN_EXHAUSTED,
    FailureClass.ABORTED,
})

# Consecutive no-verdicts on ONE attempt (same task, same retry_count) before
# the task is parked behind a gate that names the infrastructure. A verdict
# advances retry_count and so resets the count. Runner outages at the
# existing-file fetch are deliberately uncapped (DEV-620: a powered-off Mac
# lasts hours and nothing has been spent); the build-check outage keeps
# DEV-538's cap of three because it ages a generated attempt.
NO_VERDICT_CAP = int(os.getenv("AUTONOMOUS_NO_VERDICT_CAP", "5"))
_CAPS: dict[tuple[FailureClass, str], Optional[int]] = {
    (FailureClass.RUNNER_OUTAGE, "existing_fetch"): None,
    (FailureClass.RUNNER_OUTAGE, "build_check"): 3,
    # DEV-633: nothing about the prompt changes between attempts — the
    # allocator already dropped everything droppable and tried every window.
    # Retrying is N identical sums, so park on the first and name the knob
    # the operator has to move.
    (FailureClass.PROMPT_TOO_LARGE, "dispatch"): 1,
}

_MISSING_MODULE_RE = re.compile(r"No module named '([\w.]+)'")


@dataclass
class Failure:
    """One failed attempt, classified."""
    cls: FailureClass
    role: str                    # the task role that failed
    source: str = "model_call"   # model_call | parse | apply | build_check | tests | gate | runner | daemon
    detail: str = ""             # first line is the signature
    feedback: str = ""           # what a charged retry is told (verdict only)
    rotate: bool = False         # no-verdict: pick a different agent next time
    exc_type: str = ""
    phase: str = ""              # free text for the event (e.g. "existing_fetch")
    charge_role: str = ""        # verdict: whose budget pays (defaults to role)
    extra: dict = field(default_factory=dict)

    @property
    def outcome(self) -> Outcome:
        if self.cls in NO_VERDICT_CLASSES:
            return Outcome.NO_VERDICT
        if self.cls in VERDICT_CLASSES:
            return Outcome.VERDICT
        return Outcome.TERMINAL

    @property
    def signature(self) -> str:
        first = (self.detail or "").strip().splitlines()[0] if (self.detail or "").strip() else ""
        return f"{self.cls.value}:{first[:140]}"

    @property
    def cap(self) -> Optional[int]:
        if self.cls is FailureClass.SHUTDOWN:
            return None
        return _CAPS.get((self.cls, self.phase), NO_VERDICT_CAP)


@dataclass
class Disposition:
    action: str      # requeue | rotate | charge | synthesize | park | terminal | handled
    failure: Failure
    detail: str = ""
    consecutive: int = 0


@dataclass
class Hooks:
    """What dispose needs from the daemon and must not import.

    ``synthesize(db, spec, impl_task, reviewer_task, feedback)`` is the
    exhaustion escape hatch: it runs synthesis and either opens the release
    gate or returns a terminal Failure. ``supervisor(db, spec, task,
    failure)`` returns True when the supervisor Strategy handled the
    failure (only consulted for gate rejections and test failures, the two
    places it was ever consulted). ``max_retries`` is read at call time so
    tests can pin it.
    """
    max_retries: Callable[[], int]
    synthesize: Optional[Callable[..., "Failure | None"]] = None
    supervisor: Optional[Callable[..., bool]] = None
    reviewer_parse_retries: Callable[[], int] = lambda: 1


# ── classification ───────────────────────────────────────────────────────────

def classify_exception(exc: BaseException, *, role: str,
                       source: str = "model_call", phase: str = "") -> Failure:
    """Sort an exception out of a runner into a Failure."""
    name = type(exc).__name__
    if name == "ShutdownRequested":
        return Failure(FailureClass.SHUTDOWN, role, "daemon", str(exc),
                       exc_type=name, phase=phase)
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return Failure(FailureClass.TRANSPORT, role, source, f"{name}: {exc}",
                       exc_type=name, phase=phase)
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        # A 4xx is about THIS request against THIS agent (413: the prompt
        # does not fit its window) — rotate. A 5xx is the server mid-crash —
        # the same agent will do once it is back.
        rotate = status is not None and 400 <= int(status) < 500
        return Failure(FailureClass.HTTP_REFUSAL, role, source,
                       f"HTTP {status or '?'}: {exc}", rotate=rotate,
                       exc_type=name, phase=phase,
                       extra={"status": status})
    if isinstance(exc, PromptTooLarge) or name == "PromptTooLarge":
        # Raised by the budget allocator BEFORE the call: no window holds the
        # prompt even stripped of every droppable section. Nothing has been
        # spent and nothing was judged (DEV-633).
        return Failure(FailureClass.PROMPT_TOO_LARGE, role, "daemon", str(exc),
                       rotate=False, exc_type=name, phase="dispatch")
    if isinstance(exc, RunnerOutage) or name in (
            "RunnerOutageAtImplement", "RunnerOutage"):
        return Failure(FailureClass.RUNNER_OUTAGE, role, "runner", str(exc),
                       exc_type=name, phase=phase or "existing_fetch")
    if isinstance(exc, RuntimeError) and "missing choices" in str(exc):
        return Failure(FailureClass.SERVER_MALFORMED, role, source, str(exc),
                       rotate=False, exc_type=name, phase=phase)
    return Failure(FailureClass.UNKNOWN_EXCEPTION, role, "daemon",
                   f"{name}: {exc}", exc_type=name, phase=phase)


def classify_model_output(raw: str, meta: Optional[dict], *, role: str,
                          parse_reason: Optional[str] = None,
                          strip_thinking: Optional[Callable[[str], str]] = None,
                          phase: str = "") -> Optional[Failure]:
    """Judge a completion that came back but did not parse (or did not apply).

    Truncation and an empty completion are no-verdict: the model never got
    to answer. A parse failure of a real answer is a verdict. Returns None
    when nothing is wrong with the output itself.
    """
    meta = meta or {}
    visible = (strip_thinking(raw) if strip_thinking else raw) or ""
    if meta.get("truncated"):
        return Failure(FailureClass.TRUNCATED, role, "model_call",
                       f"finish_reason=length at max_tokens={meta.get('max_tokens', '?')} "
                       f"(agent={meta.get('agent', '?')}); {parse_reason or 'output cut off'}",
                       rotate=True, phase=phase,
                       extra={"agent": meta.get("agent"),
                              "max_tokens": meta.get("max_tokens")})
    if not visible.strip():
        return Failure(FailureClass.EMPTY_COMPLETION, role, "model_call",
                       f"empty completion ({len(raw or '')} raw chars, "
                       f"agent={meta.get('agent', '?')})", rotate=True, phase=phase,
                       extra={"agent": meta.get("agent")})
    if parse_reason:
        return Failure(FailureClass.PARSE_FAILURE, role, "parse", parse_reason,
                       phase=phase)
    return None


def repo_packages(paths: Iterable[str]) -> set[str]:
    """Top-level importable package names implied by repo-relative paths:
    ``src/<pkg>/...`` and ``<pkg>/...py`` both name ``<pkg>``."""
    out: set[str] = set()
    for p in paths:
        parts = [x for x in str(p).replace("\\", "/").split("/") if x]
        if len(parts) >= 2 and parts[0] == "src":
            out.add(parts[1])
        elif len(parts) >= 2 and parts[-1].endswith(".py"):
            out.add(parts[0])
    return {x for x in out if re.match(r"^[A-Za-z_]\w*$", x)}


def classify_test_run(output: str, *, role: str, passed: bool,
                      build_reason: Optional[str], unreachable: bool,
                      packages: Iterable[str] = (), phase: str = "build_check",
                      feedback: str = "") -> Optional[Failure]:
    """Judge a test dispatch. ``unreachable`` and ``build_reason`` are the
    daemon's own detectors, passed in so this stays import-free."""
    if unreachable:
        return Failure(FailureClass.RUNNER_OUTAGE, role, "runner",
                       (output or "").strip().splitlines()[0] if output else "runner unreachable",
                       phase=phase)
    if build_reason:
        m = _MISSING_MODULE_RE.search(build_reason) or _MISSING_MODULE_RE.search(output or "")
        if m:
            top = m.group(1).split(".")[0]
            if top in set(packages):
                return Failure(FailureClass.SANDBOX_PROVISIONING, role, "sandbox",
                               f"the sandbox cannot import the repository's own "
                               f"package {top!r}: {build_reason}", phase=phase,
                               extra={"module": m.group(1)})
        return Failure(FailureClass.BUILD_FAILURE, role, "build_check",
                       build_reason, feedback=feedback, phase=phase)
    if not passed:
        return Failure(FailureClass.TESTS_FAILED, role, "tests",
                       (output or "").strip().splitlines()[-1] if output else "tests failed",
                       feedback=feedback, phase=phase)
    return None


# ── disposition ──────────────────────────────────────────────────────────────

_ROLE_ORDER = {"architect": 0, "implementer": 1, "reviewer": 2}
_TERMINAL_TASK = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED)


def _payload(ev: Any) -> dict:
    try:
        return json.loads(getattr(ev, "payload_json", None) or "{}")
    except (TypeError, ValueError):
        return {}


def _classified_events(db: Any, spec_id: str, task_id: str) -> list[dict]:
    try:
        events = db.list_events_by_kind(spec_id=spec_id,
                                        kind=EventKind.FAILURE_CLASSIFIED,
                                        limit=500)
    except Exception:
        return []
    out = []
    for ev in events:  # newest first
        if getattr(ev, "task_id", None) != task_id:
            continue
        out.append(_payload(ev))
    return out


def consecutive_no_verdicts(db: Any, spec_id: str, task) -> int:
    """No-verdict dispositions already recorded for this attempt."""
    n = 0
    for p in _classified_events(db, spec_id, task.id):
        if p.get("retry") != task.retry_count:
            continue
        if p.get("outcome") != Outcome.NO_VERDICT.value:
            continue
        if p.get("cls") == FailureClass.SHUTDOWN.value:
            continue
        n += 1
    return n


def rotation_offset(db: Any, spec_id: str, task) -> int:
    """How many no-verdict dispositions on this attempt asked for a
    different agent — added to retry_count when the rotation picks, so a
    413 or a truncation moves the dispatch without spending the budget."""
    return sum(1 for p in _classified_events(db, spec_id, task.id)
               if p.get("retry") == task.retry_count
               and p.get("outcome") == Outcome.NO_VERDICT.value
               and p.get("rotate"))


def _record(db: Any, spec: Any, task: Any, failure: Failure, action: str,
            consecutive: int, detail: str = "") -> None:
    payload = {
        "role": failure.role, "outcome": failure.outcome.value,
        "cls": failure.cls.value, "source": failure.source,
        "detail": (failure.detail or "")[:600], "signature": failure.signature,
        "retry": getattr(task, "retry_count", None), "consecutive": consecutive,
        "cap": failure.cap, "disposition": action, "rotate": failure.rotate,
        "exc_type": failure.exc_type, "phase": failure.phase,
    }
    if detail:
        payload["disposition_detail"] = detail[:300]
    payload.update({k: v for k, v in failure.extra.items() if v is not None})
    db.record_event(EventKind.FAILURE_CLASSIFIED, spec_id=spec.id,
                    task_id=getattr(task, "id", None), payload=payload)


def close_spec_tasks(db: Any, spec_id: str, failed_task_id: Optional[str]) -> list[str]:
    """DEV-532: a terminal spec leaves no task row claiming to be in flight."""
    closed = []
    for t in db.list_tasks_for_spec(spec_id):
        if t.status in _TERMINAL_TASK:
            continue
        if t.id == failed_task_id or t.status in (TaskStatus.RUNNING,
                                                  TaskStatus.BLOCKED_ON_REVIEW):
            db.update_task_status(t.id, TaskStatus.FAILED)
        else:
            db.update_task_status(t.id, TaskStatus.SKIPPED)
        closed.append(t.id)
    return closed


def terminate(db: Any, spec: Any, task: Any, failure: Failure) -> Disposition:
    """The one terminal branch: fail the task, close every other task, fail
    the spec, record why."""
    logger.error("spec %s: TERMINAL (%s) — %s", spec.id, failure.cls.value,
                 failure.detail.splitlines()[0] if failure.detail else "")
    task_id = getattr(task, "id", None)
    if task_id:
        db.update_task_status(task_id, TaskStatus.FAILED)
    closed = close_spec_tasks(db, spec.id, task_id)
    db.update_spec_status(spec.id, SpecStatus.FAILED)
    _record(db, spec, task, failure, "terminal", 0,
            detail=f"closed {len(closed)} task(s)")
    return Disposition("terminal", failure, f"closed {len(closed)} task(s)")


_GATE_HEADINGS = {
    FailureClass.PARSE_FAILURE: "## Automated parse-failure retry",
    FailureClass.UNAPPLIABLE_EDITS: "## Automated unappliable-edit retry (DEV-581)",
    FailureClass.BUILD_FAILURE: "## Automated build-failure retry (DEV-429)",
    FailureClass.TESTS_FAILED: "## Automated test failure — retry",
    FailureClass.REVIEW_REJECTED: "## Release rejection — retry",
}


def _park(db: Any, spec: Any, task: Any, failure: Failure, consecutive: int) -> Disposition:
    """Too many no-verdicts on one attempt: a human decides whether the
    infrastructure is back. Approve (any notes) re-runs the task; reject
    aborts. Reuses the task-bound CLARIFICATION gate the execution gate
    handler already understands."""
    db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
    db.create_gate(
        spec_id=spec.id, task_id=task.id, gate_type=GateType.CLARIFICATION,
        prompt_md=(
            f"## Infrastructure gate: {failure.cls.value} ×{consecutive} on the "
            f"{failure.role} (DEV-629)\n\n"
            f"Spec ID: `{spec.id}`\n\n"
            f"The {failure.role}'s attempt {task.retry_count} has hit "
            f"{consecutive} consecutive no-verdict failures of class "
            f"`{failure.cls.value}` — nothing it produced was ever judged, so "
            f"no retry was charged. The last one:\n\n"
            f"```\n{(failure.detail or '')[:1500]}\n```\n\n"
            f"Approve (with any notes) once the infrastructure is back to re-run "
            f"the {failure.role} at the same attempt, or reject to abort the spec."
        ),
    )
    _record(db, spec, task, failure, "park", consecutive)
    logger.error("spec %s: %s no-verdict ×%d on %s attempt %d — parked behind an "
                 "infrastructure gate", spec.id, failure.cls.value, consecutive,
                 failure.role, task.retry_count)
    return Disposition("park", failure, consecutive=consecutive)


def dispose(db: Any, spec: Any, task: Any, failure: Failure, hooks: Hooks,
            *, reviewer_task: Any = None) -> Disposition:
    """Act on a classified failure. See the module docstring."""
    outcome = failure.outcome
    if outcome is Outcome.TERMINAL:
        return terminate(db, spec, task, failure)

    if outcome is Outcome.NO_VERDICT:
        consecutive = consecutive_no_verdicts(db, spec.id, task) + 1
        cap = failure.cap
        if cap is not None and consecutive > cap:
            return _park(db, spec, task, failure, consecutive)
        action = "rotate" if failure.rotate else "requeue"
        db.update_task_status(task.id, TaskStatus.PENDING)
        _record(db, spec, task, failure, action, consecutive)
        logger.warning("spec %s: no-verdict %s on %s (%s) — %s without charging "
                       "(attempt still %d/%d; %d/%s consecutive)", spec.id,
                       failure.cls.value, failure.role, failure.source, action,
                       task.retry_count, hooks.max_retries(), consecutive,
                       cap if cap is not None else "∞")
        return Disposition(action, failure, consecutive=consecutive)

    # ── verdict ──────────────────────────────────────────────────────────
    if hooks.supervisor is not None and failure.source in ("gate", "tests"):
        try:
            if hooks.supervisor(db, spec, task, failure):
                _record(db, spec, task, failure, "supervisor", 0)
                return Disposition("handled", failure, "supervisor")
        except Exception:
            logger.warning("spec %s: supervisor strategy raised; using the default "
                           "disposition", spec.id, exc_info=True)

    charge_role = failure.charge_role or failure.role
    max_retries = hooks.max_retries()

    if charge_role == "architect":
        if task.retry_count >= max_retries:
            return terminate(db, spec, task, Failure(
                FailureClass.DESIGN_EXHAUSTED, "architect", failure.source,
                f"architect budget exhausted ({task.retry_count}/{max_retries}) "
                f"after {failure.cls.value}: {failure.detail}"))
        if failure.feedback:
            try:
                (db.spec_dir(spec.id) / "design_review_feedback.md").write_text(
                    failure.feedback)
            except OSError as e:
                logger.warning("spec %s: could not persist design feedback: %s",
                               spec.id, e)
        db.increment_task_retry(task.id)
        db.update_task_status(task.id, TaskStatus.PENDING)
        _record(db, spec, task, failure, "charge", 0)
        logger.info("spec %s: architect charged for %s (retry %d/%d)", spec.id,
                    failure.cls.value, task.retry_count + 1, max_retries)
        return Disposition("charge", failure)

    if charge_role == "reviewer":
        # A reviewer verdict is charged to the reviewer's own small budget of
        # re-runs; past it the implementer pays, as before (soft FAIL).
        if task.retry_count < hooks.reviewer_parse_retries():
            db.increment_task_retry(task.id)
            db.update_task_status(task.id, TaskStatus.PENDING)
            _record(db, spec, task, failure, "charge", 0)
            return Disposition("charge", failure, "reviewer re-run")
        failure = Failure(FailureClass.TESTS_FAILED, "reviewer", "tests",
                          failure.detail, feedback=failure.feedback,
                          charge_role="implementer", phase=failure.phase)
        charge_role = "implementer"

    # implementer pays (its own failure, or one found at the reviewer stage)
    impl_task = task if task.role == "implementer" else None
    if impl_task is None:
        impls = db.list_tasks_for_spec_by_role(spec.id, "implementer")
        impl_task = impls[0] if impls else None
    if impl_task is None:
        return terminate(db, spec, task, Failure(
            FailureClass.ABORTED, failure.role, failure.source,
            "no implementer task to charge"))
    if reviewer_task is None and task.role == "reviewer":
        reviewer_task = task
    if reviewer_task is None:
        revs = db.list_tasks_for_spec_by_role(spec.id, "reviewer")
        reviewer_task = revs[0] if revs else None

    if impl_task.retry_count >= max_retries:
        # The escape hatch (DEV-433): merge the attempts on disk, test the
        # merge, and either open a release gate or end the spec — for EVERY
        # verdict class, parse failures included.
        _record(db, spec, impl_task, failure, "synthesize", 0)
        if hooks.synthesize is None or reviewer_task is None:
            return terminate(db, spec, impl_task, Failure(
                FailureClass.SYNTHESIS_FAILED, "implementer", failure.source,
                f"budget exhausted ({impl_task.retry_count}/{max_retries}) and "
                f"no synthesis available"))
        terminal = hooks.synthesize(db, spec, impl_task, reviewer_task,
                                    failure.feedback or failure.detail)
        if terminal is not None:
            return terminate(db, spec, impl_task, terminal)
        return Disposition("synthesize", failure)

    heading = _GATE_HEADINGS.get(failure.cls, "## Automated retry")
    gate = db.create_gate(spec_id=spec.id, task_id=impl_task.id,
                          gate_type=GateType.CODE_REVIEW, prompt_md=heading)
    db.respond_to_gate(gate.id, "rejected",
                       notes=failure.feedback or failure.detail or heading)
    db.increment_task_retry(impl_task.id)
    db.update_task_status(impl_task.id, TaskStatus.PENDING)
    if reviewer_task is not None and reviewer_task.id != impl_task.id:
        # Re-read the row: the object a runner hands us was fetched before
        # claim_task moved it to RUNNING, so its status is stale, and a
        # reviewer left RUNNING here trips a spurious crash recovery later.
        fresh = db.get_task(reviewer_task.id) or reviewer_task
        if fresh.status not in (TaskStatus.PENDING, TaskStatus.SKIPPED):
            db.update_task_status(reviewer_task.id, TaskStatus.PENDING)
    _record(db, spec, impl_task, failure, "charge", 0)
    logger.info("spec %s: implementer charged for %s (%s) — rotating "
                "(attempt %d/%d)", spec.id, failure.cls.value, failure.source,
                impl_task.retry_count + 1, max_retries)
    return Disposition("charge", failure)
