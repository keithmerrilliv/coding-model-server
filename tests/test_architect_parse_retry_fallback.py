"""A parse-flaky architect REVISION must not discard a valid prior design — DEV-543.

On Centipede slice 5 (spec_1ff33048, 2026-08-13) the architect produced a valid
design twice, but each was bounced (design_review FAIL, then a testability
finding), and on the third revision cycle every parse-retry attempt failed. The
whole spec then FAILED at design — throwing away a design that had already
parsed and been reviewed — before the implementer ever ran.

The fix: when the parse-retry loop exhausts on a REVISION cycle (retry_count > 0)
and a prior design.md exists, fall back to it and send it to the human gate
instead of failing the spec. The fail behavior is preserved when there is
genuinely nothing to carry forward (first cycle, or design.md absent).
"""
import textwrap
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ParseError
from coding_model_autonomous.models import GateType, SpecStatus, TaskStatus

PLAN = textwrap.dedent("""\
    title: "demo"
    language: swift
    test_strategy:
      framework: swift_test
      required: true
    phases:
      - name: design
        role: architect
""")
SPEC = "# Demo\n\nBuild it.\n"
PRIOR_DESIGN = "# Architecture: Demo\n\n## Overview\nA valid design from a prior cycle.\n"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _spec(db, retry_count=0):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING, normalized_yaml=PLAN)
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text(SPEC)
    (spec_dir / "plan.yaml").write_text(PLAN)
    task = db.create_task(spec_id=spec.id, agent="architect",
                          role="architect", title="design demo")
    for _ in range(retry_count):
        db.increment_task_retry(task.id)
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _all_parse_errors():
    return mock.patch.object(
        d, "parse_architect_response",
        return_value=ParseError(reason="no <<<DESIGN>>> marker", raw="prose"))


# ── the fix ─────────────────────────────────────────────────────────────────

def test_parse_exhaustion_on_a_revision_falls_back_to_the_prior_design(db):
    """A revision whose every attempt fails to parse ships the last-good design
    to the gate rather than failing the spec (DEV-543)."""
    spec, task, spec_dir = _spec(db, retry_count=1)
    (spec_dir / "design.md").write_text(PRIOR_DESIGN)

    with mock.patch.object(d, "call_agent", return_value="prose"), \
            _all_parse_errors():
        d._run_architect(db, spec, task, spec_dir)

    assert db.get_spec(spec.id).status is not SpecStatus.FAILED
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    gates = [g for g in db.list_gates_for_spec(spec.id)
             if g.gate_type is GateType.DESIGN_APPROVAL]
    assert len(gates) == 1, "the last-good design must reach a design gate"
    assert "A valid design from a prior cycle." in gates[0].prompt_md
    assert "failed to parse" in gates[0].prompt_md


# ── the fix must not become a catch-all ─────────────────────────────────────

def test_parse_exhaustion_on_the_first_cycle_still_fails(db):
    """No prior design (retry_count 0) means nothing to fall back to — the spec
    must still FAIL rather than gate an absent design."""
    spec, task, spec_dir = _spec(db, retry_count=0)
    assert not (spec_dir / "design.md").exists()

    with mock.patch.object(d, "call_agent", return_value="prose"), \
            _all_parse_errors():
        d._run_architect(db, spec, task, spec_dir)

    assert db.get_spec(spec.id).status is SpecStatus.FAILED


def test_parse_exhaustion_on_a_revision_with_no_prior_design_still_fails(db):
    """A revision cycle but design.md is missing: genuinely nothing to carry
    forward, so the original fail behavior is preserved."""
    spec, task, spec_dir = _spec(db, retry_count=1)
    assert not (spec_dir / "design.md").exists()

    with mock.patch.object(d, "call_agent", return_value="prose"), \
            _all_parse_errors():
        d._run_architect(db, spec, task, spec_dir)

    assert db.get_spec(spec.id).status is SpecStatus.FAILED
