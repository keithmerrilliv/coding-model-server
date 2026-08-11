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

import logging
import hashlib
import shutil
from pathlib import Path

from . import supervisor as _supervisor
from .db import Database
from .executor import ALLOWED_IMPLEMENTER_AGENTS, TIER_TO_IMPLEMENTER
from .models import EventKind

logger = logging.getLogger("orchestrator.retry_policy")


_PRESERVE_ON_RETRY: frozenset[str] = frozenset({
    # Pipeline inputs / outputs of earlier phases — must persist so the
    # next implementer sees the same plan + design + complexity decision.
    "spec.md", "plan.yaml", "design.md", "complexity.json",
    # Diagnostic artifacts from prior runs. Not used as inputs (rejection
    # notes come from the gate, not these files), but useful for postmortem.
    "failure_report.md", "review_report.md", "test_output.txt",
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

    def _gather(root: Path, retry_index: int) -> dict:
        files: dict[str, str] = {}
        test_output = ""
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            # Skip anything under retry_history — the snapshots from prior
            # retries are surfaced as separate entries already.
            if rel.parts and rel.parts[0] == "retry_history":
                continue
            name = rel.name
            # Drop pipeline metadata; keep only deliverable code + tests.
            if name in {"spec.md", "plan.yaml", "design.md", "complexity.json",
                        "review_report.md", "failure_report.md"}:
                continue
            # Only the spec_dir-level test_output.txt counts (not any
            # snapshot copy, which we already filtered above).
            if name == "test_output.txt" and rel.parent == Path():
                test_output = path.read_text(errors="replace")
                continue
            try:
                files[str(rel)] = path.read_text(errors="replace")
            except OSError:
                continue
        # DEV-553: the snapshot already contains the design.md that was
        # current when this attempt ran, because _snapshot_retry copies the
        # whole spec_dir. Digest it so synthesis can tell which attempts were
        # written against a design the architect has since revised. No new
        # state to thread — the evidence was already on disk.
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
