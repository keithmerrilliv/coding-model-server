"""Replay run 3's real artifacts through the new paths — DEV-468 / DEV-469.

These do not use fixtures. They read the actual failure output that
spec_cc7dd609 produced on 2026-08-03 and assert the new logic fires on it.
That matters because the first cut of DEV-468 keyed on whole-signature
equality and, verified against this same data, would never have fired: no two
consecutive attempts had identical error *sets*, even though the design-caused
diagnostics persisted through four of the five.

Skipped rather than failed when the artifacts are gone, so the suite still
passes on a machine without that run's workspace.
"""
import pathlib

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import (
    GateType, SpecStatus, TaskStatus,
)

RUN3 = pathlib.Path("var/tasks_db/specs/spec_cc7dd609")
RETRIES = RUN3 / "retry_history"

pytestmark = pytest.mark.skipif(
    not RETRIES.is_dir(), reason="run 3 artifacts not present on this machine")


def _build_failures():
    """(name, text) for each retry that recorded a build failure, in order."""
    out = []
    for r in sorted(RETRIES.glob("retry_*"),
                    key=lambda p: int(p.name.split("_")[1])):
        f = r / "build_failure.txt"
        if f.is_file():
            out.append((r.name, f.read_text()))
    return out


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


# ── DEV-468: the design-caused diagnostics must be detectable ────────────────

def test_run3_had_no_two_consecutive_identical_signatures():
    """Pins why the first implementation was inert — this is the regression."""
    sigs = [d._failure_signature(t) for _, t in _build_failures()]
    assert len(sigs) >= 2
    assert all(a != b for a, b in zip(sigs, sigs[1:])), (
        "if this ever becomes false, whole-signature equality would work and "
        "this test is no longer pinning anything")


def test_design_caused_diagnostics_persist_across_run3():
    """The signal the fix actually keys on."""
    msg_sets = [d._diagnostic_messages(t) for _, t in _build_failures()]
    mutating = "'mutating' is not valid on instance methods in classes"
    seen = sum(1 for s in msg_sets if mutating in s)
    assert seen >= 4, f"expected the design's `mutating` defect throughout, saw {seen}"


def test_consecutive_pairs_share_a_persistent_diagnostic(db):
    """Every adjacent pair in run 3 shares at least one surviving diagnostic,
    so the router would have had a signal at each step."""
    failures = _build_failures()
    spec = db.create_spec(title="r3", source_md_path="spec.md")
    fired = 0
    for i, (name, text) in enumerate(failures):
        if i:
            persistent = d._persistent_diagnostics(db, spec.id, text, lookback=1)
            if persistent:
                fired += 1
        gate = db.create_gate(spec_id=spec.id, task_id=None,
                              gate_type=GateType.CODE_REVIEW,
                              prompt_md="## Automated build-failure retry (DEV-429)")
        db.respond_to_gate(gate.id, "rejected", notes=text)
    assert fired >= 3, f"router would have fired on only {fired} of 4 transitions"


def test_router_fires_on_run3s_second_failure(db):
    """End to end: replay attempt 1 then attempt 2, assert we go upstream."""
    failures = _build_failures()
    spec = db.create_spec(title="r3", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    arch = db.create_task(spec_id=spec.id, agent="architect", role="architect",
                          title="design")
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)

    # attempt 1 recorded exactly as the build-failure path records it
    first = db.create_gate(spec_id=spec.id, task_id=None,
                           gate_type=GateType.CODE_REVIEW,
                           prompt_md="## Automated build-failure retry (DEV-429)")
    db.respond_to_gate(first.id, "rejected", notes=failures[0][1])

    # attempt 2 arrives; pick the next one sharing a diagnostic with attempt 1
    shared = None
    for _, text in failures[1:]:
        if d._diagnostic_messages(text) & d._diagnostic_messages(failures[0][1]):
            shared = text
            break
    assert shared is not None, "run 3 had no adjacent overlap to replay"

    routed = d._route_build_failure_to_architect(
        db, db.get_spec(spec.id), db.get_task(impl.id), spec_dir,
        shared, "first diagnostic")
    assert routed is True
    assert db.get_task(arch.id).status is TaskStatus.PENDING
    assert db.get_task(arch.id).retry_count == 1
    assert db.get_task(impl.id).retry_count == 0  # not charged

    fb = (spec_dir / "design_review_feedback.md").read_text()
    assert "cannot be built as written" in fb
    assert "survived" in fb


def test_a_genuinely_new_failure_does_not_route(db):
    """Progress must still look like progress."""
    spec = db.create_spec(title="r3", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    db.create_task(spec_id=spec.id, agent="architect", role="architect", title="d")
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="b")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    g = db.create_gate(spec_id=spec.id, task_id=None,
                       gate_type=GateType.CODE_REVIEW, prompt_md="## Automated")
    db.respond_to_gate(g.id, "rejected", notes=_build_failures()[0][1])

    assert d._route_build_failure_to_architect(
        db, db.get_spec(spec.id), db.get_task(impl.id), spec_dir,
        "X.swift:1:1: error: something entirely new and unrelated",
        "new") is False


# ── DEV-469: the real synthesis failure must be repair-eligible ──────────────

def test_run3_synthesis_output_is_recognised_as_a_build_failure():
    out = RUN3 / "test_output.txt"
    if not out.is_file():
        pytest.skip("synthesis output not present")
    text = out.read_text()
    reason = d._detect_build_failure(text, "swift_test", False)
    assert reason is not None, "the real synthesis failure must be repair-eligible"
    assert d._test_pass_rate(text) is None, "no runner summary — the old gate"
    # the two type errors that ended run 3
    assert "Int64" in text
