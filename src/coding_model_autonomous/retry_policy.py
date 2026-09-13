"""Retry policy for autonomous specs: what to preserve, what to wipe, who retries.

Extracted from orchestrator_daemon.py (DEV-152). These are the decision and
state-reading helpers of the retry path — snapshotting a failed attempt,
deciding which artifacts survive a wipe, rotating the implementer agent, and
reading back prior attempts and supervisor directives. None of them touch the
daemon's globals, so they are now exercisable without importing the daemon and
triggering its import-time load_dotenv()/basicConfig().

Deliberately NOT moved: _attempt_retry, _legacy_attempt_retry and
_retry_role_with_feedback. Those drive the state machine — they call
_apply_supervisor_decision, _build_supervisor_context and _run_synthesis — so
moving them would either drag half the daemon along or introduce an import
cycle. This module has no edges back into the daemon.
"""
from __future__ import annotations

import json
import logging
import hashlib
import os
import random
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional

from . import outcome as _outcome
from . import supervisor as _supervisor
from .db import Database
from .executor import ALLOWED_IMPLEMENTER_AGENTS, TIER_TO_IMPLEMENTER
from .models import EventKind
from .context import CONTEXT_FILE
from .workspace import LEDGER_FILE, attempt_files_from_ledger, read_entries

logger = logging.getLogger("orchestrator.retry_policy")


_PRESERVE_ON_RETRY: frozenset[str] = frozenset({
    # Pipeline inputs / outputs of earlier phases — must persist so the
    # next implementer sees the same plan + design + complexity decision.
    "spec.md", "plan.yaml", "design.md", "complexity.json",
    # Diagnostic artifacts from prior runs. Not used as inputs (rejection
    # notes come from the gate, not these files), but useful for postmortem.
    "failure_report.md", "review_report.md", "test_output.txt",
    # DEV-642: the artifact ledger is the record of every attempt's writes
    # and of the repository baselines; the synthesis corpus is built from it.
    LEDGER_FILE,
    # DEV-632: the context stage's fetch — what every role selected from;
    # re-fetching it per retry is what the stage exists to stop.
    CONTEXT_FILE,
})

# Run diagnostics that live beside the code in a workspace and must never be
# offered to synthesis as "attempt code" (DEV-639). Dotted top-level
# directories (.repo_overlay, .pytest_cache) are excluded by shape.
_CORPUS_DENYLIST: frozenset[str] = frozenset({
    "spec.md", "plan.yaml", "design.md", "complexity.json",
    "review_report.md", "failure_report.md", "implementer_response.md",
    "build_check_output.txt", "build_failure.txt", "build_warnings.txt",
    "design_review.md", "design_review_feedback.md", "human_design_feedback.md",
    "tested_manifest.json", "manifest.json", "delivery_report.md",
    "reviewer_failed_response.txt", LEDGER_FILE,
})


def _snapshot_retry(spec_dir: Path, retry_index: int) -> None:
    """Copy the current state of spec_dir into retry_history/retry_<N>/
    (excluding retry_history itself). Called BEFORE cleanup so each retry's
    output is preserved for the synthesis pass.
    """
    snap = spec_dir / "retry_history" / f"retry_{retry_index}"
    snap.mkdir(parents=True, exist_ok=True)
    for path in spec_dir.iterdir():
        if path.name == "retry_history":
            continue
        target = snap / path.name
        try:
            if path.is_dir():
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                shutil.copy2(path, target)
        except OSError as exc:
            logger.warning("snapshot retry_%d: failed to copy %s: %s",
                           retry_index, path, exc)


def _clean_spec_dir_for_retry(spec_dir: Path, retry_count: int) -> None:
    """Wipe implementer / reviewer artifacts from spec_dir, keeping the
    inputs (spec, plan, design) and prior-run diagnostics.

    Snapshots the prior retry's state into retry_history/retry_<N-1>/
    BEFORE wiping, so the synthesis pass at MAX_RETRIES has the full
    rotation corpus to work from.

    Why: the orchestrator does not isolate retries — earlier retries leave
    files (implementer code, reviewer-written pytest tests) behind. When
    retry-N picks a different file layout than retry-(N-1) (e.g. flat
    `test_*.py` vs `tests/test_*.py`), pytest sees duplicate module names
    and aborts collection with `import file mismatch` before any test
    actually runs. Surfaced 2026-05-04 in spec_099515d1 retry-1; see
    project_orchestrator_spec_dir_contamination.md.

    Called at the start of _run_implementer for retry_count > 0. retry-0
    runs against an empty (post-design) spec dir so cleanup is a no-op.
    """
    if retry_count == 0:
        return
    _snapshot_retry(spec_dir, retry_count - 1)
    removed = 0
    for path in spec_dir.iterdir():
        if path.name in _PRESERVE_ON_RETRY or path.name == "retry_history":
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("spec dir cleanup: failed to remove %s: %s",
                           path, exc)
    logger.info(
        "spec dir cleaned for retry=%d (snapshot retry_%d, %d items removed, %d preserved)",
        retry_count, retry_count - 1, removed, len(_PRESERVE_ON_RETRY),
    )


def _select_implementer_agent(spec_dir) -> "str | None":
    """Read complexity.json and pick the implementer agent.

    Precedence: architect's specific `recommended_agent` (if it's in the
    whitelist) → tier default (if tier is recognized) → None (caller uses the
    env-default IMPLEMENTER_AGENT). Returns None silently on any error so a
    malformed or absent complexity.json never blocks the pipeline.
    """
    import json as _json
    cpath = spec_dir / "complexity.json"
    if not cpath.exists():
        return None
    try:
        c = _json.loads(cpath.read_text())
    except (OSError, _json.JSONDecodeError):
        return None
    rec = (c.get("recommended_agent") or "").strip()
    if rec in ALLOWED_IMPLEMENTER_AGENTS:
        return rec
    tier = (c.get("tier") or "").strip().lower()
    return TIER_TO_IMPLEMENTER.get(tier)


# Rotation chain for implementer retries. Ordered for vendor/family diversity:
# Qwen3.6 → Qwen3-Coder-Next → MiniMax → Qwen3-Coder. GLM (Zhipu) was removed
# 2026-06-07: as a reasoning model it burns the per-file/manifest budget on
# reasoning (even with --reasoning-budget 0, which it ignores — see config.py)
# and truncates without emitting usable code. See project_implementer_rotation.md
# and project_glm_perfile_truncation.md for the evidence.
_IMPLEMENTER_ROTATION = [
    "implementer", "deep_implementer",
    "moe_implementer", "fast_implementer",
]


def _rotation_pick(initial_agent: "str | None", retry_count: int) -> "str | None":
    """Advance to the next implementer in the rotation chain on retry.

    Retry 0 returns ``initial_agent`` unchanged — the architect's complexity
    recommendation wins on first attempt. From retry 1 onward, walks the
    rotation chain so each retry uses a different model and we get out of
    any single-model fragility pattern (see
    project_implementer_revision_fragility.md).

    The chain starts with ``initial_agent`` so retry index N maps to the
    Nth agent in a stable order; consecutive retries never repeat the same
    model.
    """
    if retry_count == 0 or not initial_agent:
        return initial_agent
    if initial_agent in _IMPLEMENTER_ROTATION:
        chain = [initial_agent] + [a for a in _IMPLEMENTER_ROTATION if a != initial_agent]
    else:
        chain = _IMPLEMENTER_ROTATION
    return chain[retry_count % len(chain)]


# ── DEV-530 option 1: a stated fraction of attempts is assigned at random ────
# Rotation is failure-triggered, so position in the rotation and attempt
# difficulty are the same variable and per-agent rates rank agents
# backwards. On this fraction of dispatches the agent is drawn uniformly
# from the rotation instead, which decouples the two. 0 (the default) means
# the shipped behaviour exactly; the assignment is recorded on the
# ATTEMPT_PLANNED event either way, so stratified reads can tell them apart.
ROTATION_RANDOM_FRACTION = float(
    os.getenv("AUTONOMOUS_ROTATION_RANDOM_FRACTION", "0") or 0)
_rng = random.Random()


def random_rotation_pick() -> Optional[str]:
    """An agent drawn uniformly from the rotation, on ROTATION_RANDOM_FRACTION
    of calls; None otherwise (use the rotation)."""
    if ROTATION_RANDOM_FRACTION <= 0 or not _IMPLEMENTER_ROTATION:
        return None
    if _rng.random() >= ROTATION_RANDOM_FRACTION:
        return None
    return _rng.choice(list(_IMPLEMENTER_ROTATION))


# ── DEV-631: what will differ from the failed attempt? ───────────────────────
#
# The retry loop incremented retry_count, rotated the agent and re-dispatched;
# it had no notion of a dispatch that cannot change the outcome. `AttemptPlan`
# is what a dispatch is about to pull on — the agent, the prompt, the feedback
# in it, the temperature, the environment — recorded BEFORE the call as an
# ATTEMPT_PLANNED event with what changed since the previous attempt and why.
# A plan identical to an earlier attempt's on every lever is a dispatch that
# has already been tried; the loop injects a difference (the next agent in the
# rotation) rather than spend an attempt re-proving it. The same record is
# DEV-530's difficulty proxy: which failure preceded this attempt, produced by
# which agent, with how many diagnostics — so per-agent outcomes can be read
# at matched difficulty instead of confounded by rotation position.

_PLAN_LEVERS = ("agent", "prompt_digest", "feedback_digest", "temperature",
                "env_digest")
_ENV_KEYS = ("repo", "base_ref", "framework", "execution_target", "destination")


def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part or "").encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class AttemptPlan:
    role: str
    retry: int
    agent: Optional[str]
    prompt_digest: str            # everything prompt-shaping: design, clarifications, feedback
    feedback_digest: str          # the feedback alone ("" when none)
    temperature: float
    env_digest: str               # the test_strategy the attempt is judged in
    assignment: str = "rotation"  # recommended | rotation | random | injected | fixed
    # DEV-530 option 2 — the difficulty proxy
    prior_cls: Optional[str] = None
    prior_coarse_key: Optional[str] = None
    prior_agent: Optional[str] = None
    prior_outcome: Optional[str] = None
    diagnostics: int = 0
    feedback_chars: int = 0

    def levers(self) -> dict:
        return {k: getattr(self, k) for k in _PLAN_LEVERS}

    def changed_from(self, previous: "dict | None") -> list:
        if previous is None:
            return []
        return [k for k in _PLAN_LEVERS if previous.get(k) != getattr(self, k)]

    def same_levers(self, other: dict) -> bool:
        return all(other.get(k) == getattr(self, k) for k in _PLAN_LEVERS)


def _classified_on_task(db: Database, spec_id: str, task_id: str) -> list:
    try:
        events = db.list_events_by_kind(spec_id=spec_id,
                                        kind=EventKind.FAILURE_CLASSIFIED,
                                        limit=500)
    except Exception:
        return []
    return [_outcome._payload(ev) for ev in events
            if getattr(ev, "task_id", None) == task_id]


def plan_attempt(db: Database, spec_id: str, task, *, role: str,
                 agent: Optional[str], feedback: Optional[str],
                 prompt_inputs: Iterable[Any], strategy: "dict | None",
                 temperature: float = 0.2,
                 assignment: str = "rotation") -> AttemptPlan:
    """What this dispatch is about to pull on, and what preceded it."""
    fb = feedback or ""
    strategy = strategy if isinstance(strategy, dict) else {}
    prior: dict = {}
    for p in _classified_on_task(db, spec_id, task.id):  # newest first
        # The failure that caused attempt N was recorded at retry N-1; a
        # no-verdict requeue on THIS attempt is newer but did not cause it.
        if p.get("retry") == task.retry_count - 1:
            prior = p
            break
    if not prior and task.retry_count > 0:
        classified = _classified_on_task(db, spec_id, task.id)
        prior = classified[0] if classified else {}
    return AttemptPlan(
        role=role, retry=task.retry_count, agent=agent,
        prompt_digest=_digest(*prompt_inputs, fb),
        feedback_digest=_digest(fb) if fb else "",
        temperature=temperature,
        env_digest=_digest(json.dumps({k: strategy.get(k) for k in _ENV_KEYS},
                                      sort_keys=True, default=str)),
        assignment=assignment,
        prior_cls=prior.get("cls"), prior_coarse_key=prior.get("coarse_key"),
        prior_agent=prior.get("agent"), prior_outcome=prior.get("outcome"),
        diagnostics=len(_outcome.diagnostic_messages(fb)),
        feedback_chars=len(fb))


def previous_plans(db: Database, spec_id: str, task_id: str) -> list:
    """Newest first: the ATTEMPT_PLANNED payloads recorded for *task_id*."""
    try:
        events = db.list_events_by_kind(spec_id=spec_id,
                                        kind=EventKind.ATTEMPT_PLANNED,
                                        limit=200)
    except Exception:
        return []
    return [_outcome._payload(ev) for ev in events
            if getattr(ev, "task_id", None) == task_id]


def identical_earlier_attempt(plan: AttemptPlan, prior_plans: list) -> Optional[int]:
    """The retry index of an earlier attempt with the same levers, or None."""
    for p in prior_plans:
        if p.get("retry") == plan.retry:
            continue  # this attempt's own earlier dispatch (a requeue)
        if plan.same_levers(p):
            return p.get("retry")
    return None


def inject_difference(plan: AttemptPlan, prior_plans: list) -> Optional[AttemptPlan]:
    """When *plan* repeats an earlier attempt on every lever, the first agent
    along the rotation from *plan.agent* that makes it a dispatch nobody has
    tried — the one lever the loop owns. None when the plan already differs,
    or when every agent in the rotation has already had this exact dispatch
    (then there is nothing left to inject and the caller says so)."""
    if identical_earlier_attempt(plan, prior_plans) is None:
        return None
    if not plan.agent or len(_IMPLEMENTER_ROTATION) < 2:
        return None
    for step in range(1, len(_IMPLEMENTER_ROTATION)):
        alternative = _rotation_pick(plan.agent, step)
        if not alternative or alternative == plan.agent:
            continue
        candidate = replace(plan, agent=alternative, assignment="injected")
        if identical_earlier_attempt(candidate, prior_plans) is None:
            return candidate
    return None


def rationale_for(plan: AttemptPlan, previous: "dict | None",
                  identical_to: Optional[int]) -> str:
    if previous is None:
        return "first attempt" if plan.retry == 0 else (
            f"retry {plan.retry} with no earlier plan on record")
    after = (f"after {plan.prior_cls or 'no classified failure'}"
             + (f" ({plan.prior_coarse_key})" if plan.prior_coarse_key else "")
             + (f" by {plan.prior_agent}" if plan.prior_agent else ""))
    changed = plan.changed_from(previous)
    if plan.assignment == "injected":
        return (f"retry {plan.retry} {after}: the rotation's pick would have "
                f"repeated an earlier attempt on every lever — injected "
                f"agent {plan.agent!r} instead")
    if identical_to is not None:
        return (f"retry {plan.retry} {after}: identical to attempt "
                f"{identical_to} on every lever and no untried agent is left "
                f"in the rotation — this dispatch cannot change the outcome")
    if not changed:
        return (f"retry {plan.retry} {after}: NOTHING differs from the "
                f"previous attempt — this dispatch cannot change the outcome")
    return f"retry {plan.retry} {after}: changed {', '.join(changed)}"


def record_attempt_plan(db: Database, spec_id: str, task, plan: AttemptPlan,
                        prior_plans: "list | None" = None) -> dict:
    """Write the ATTEMPT_PLANNED event; returns its payload."""
    if prior_plans is None:
        prior_plans = previous_plans(db, spec_id, task.id)
    previous = next((p for p in prior_plans if p.get("retry") != plan.retry),
                    None)
    identical_to = identical_earlier_attempt(plan, prior_plans)
    payload = asdict(plan)
    payload.update({
        "changed": plan.changed_from(previous),
        "identical_to": identical_to,
        "rationale": rationale_for(plan, previous, identical_to),
    })
    db.record_event(EventKind.ATTEMPT_PLANNED, spec_id=spec_id,
                    task_id=task.id, payload=payload)
    return payload


def _latest_supervisor_feedback(db: Database, spec_id: str,
                                *, target_role: str) -> str | None:
    """Most recent supervisor `feedback_to_inject` for *target_role* on this spec.

    Used by `_run_reviewer` (and any role whose retry path can't carry feedback
    through a synthetic gate) to read the supervisor's directive on retry.
    Returns None if no matching decision is found.
    """
    import json
    rows = db.list_events_by_kind(
        spec_id=spec_id, kind=EventKind.SUPERVISOR_DECISION, limit=20,
    )
    for r in rows:  # most-recent first
        if not r.payload_json:
            continue
        try:
            payload = json.loads(r.payload_json)
        except json.JSONDecodeError:
            continue
        if (payload.get("action") == "retry"
                and payload.get("target_role") == target_role
                and payload.get("feedback_to_inject")):
            return payload["feedback_to_inject"]
    return None


def _load_prior_decisions(db: Database, spec_id: str) -> list[dict]:
    """Most-recent N supervisor decisions for *spec_id*, oldest-first.

    Decoded from the events table's payload_json, capped to the supervisor
    transition budget so we never render more than the model could have made.
    """
    import json
    rows = db.list_events_by_kind(
        spec_id=spec_id,
        kind=EventKind.SUPERVISOR_DECISION,
        limit=_supervisor.MAX_SUPERVISOR_TRANSITIONS,
    )
    out = []
    for r in reversed(rows):  # oldest-first for natural reading order
        if not r.payload_json:
            continue
        try:
            payload = json.loads(r.payload_json)
        except json.JSONDecodeError:
            continue
        out.append({
            "action": payload.get("action", "?"),
            "target_role": payload.get("target_role"),
            "reason": payload.get("reason", ""),
        })
    return out


def _read_retry_attempts(spec_dir: Path) -> list[dict]:
    """Walk retry_history/ + the current live spec_dir and return one dict
    per attempt for the synthesis prompt. Each dict carries:
        retry: int — 0-indexed attempt number
        agent: str — best-effort guess from the snapshot (or "current")
        files: dict[relpath, content] — implementer source (no test_*.py)
        test_summary: str — last ~1500 chars of test_output.txt for that attempt
    """
    history = spec_dir / "retry_history"
    attempts: list[dict] = []

    def _walk(root: Path) -> dict[str, str]:
        """Fallback for a workspace with no ledger: deliverable code only.

        DEV-639: the old walk kept everything not on a six-name denylist,
        which after DEV-626 meant `.repo_overlay/**` — 79 files and 1.35M
        characters of the repository's own source presented to the merge
        model as attempt code. Dotted directories and run diagnostics are
        out by shape and by name.
        """
        files: dict[str, str] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if rel.parts and (rel.parts[0] == "retry_history"
                              or rel.parts[0].startswith(".")):
                continue
            if rel.name in _CORPUS_DENYLIST or rel.name == "test_output.txt":
                continue
            if rel.name.startswith("architect_failed_response_attempt"):
                continue
            try:
                files[str(rel)] = path.read_text(errors="replace")
            except OSError:
                continue
        return files

    def _gather(root: Path, retry_index: int) -> dict:
        test_output = ""
        try:
            test_output = (root / "test_output.txt").read_text(errors="replace")
        except OSError:
            pass
        # DEV-642: the ledger names exactly what the attempt wrote. A
        # workspace without one (pre-ledger snapshots) falls back to the
        # filtered walk.
        entries = read_entries(root)
        files = None
        source = "walk"
        if entries is not None:
            files = attempt_files_from_ledger(root, entries, retry_index)
            source = "ledger" if files else "ledger-empty"
        if files is None:
            files = _walk(root)
        chars = sum(len(c) for c in files.values())
        logger.info("synthesis corpus: attempt %d from %s — %d file(s), %d chars",
                    retry_index, source, len(files), chars)
        design = root / "design.md"
        digest = ""
        if design.is_file():
            try:
                digest = hashlib.sha1(
                    design.read_bytes()).hexdigest()[:12]
            except OSError:
                pass
        return {
            "retry": retry_index,
            "agent": "snapshot",
            "files": files,
            "test_summary": test_output[-1500:] if test_output else "",
            "design_digest": digest,
        }

    if history.exists():
        for sub in sorted(history.iterdir()):
            if not sub.is_dir() or not sub.name.startswith("retry_"):
                continue
            try:
                idx = int(sub.name.split("_", 1)[1])
            except ValueError:
                continue
            attempts.append(_gather(sub, idx))

    # Include the live spec_dir as the latest attempt.
    live = _gather(spec_dir, retry_index=len(attempts))
    live["agent"] = "current"
    if live["files"]:
        attempts.append(live)
    return attempts
