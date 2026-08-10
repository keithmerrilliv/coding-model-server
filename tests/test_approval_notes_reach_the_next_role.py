"""Gate APPROVAL notes must reach the next role — DEV-546.

Rejection notes propagated and demonstrably worked: three design rejections on
run 9 each produced a design that addressed the named items. Approval notes
were stored on the gate row, mirrored to Jira, and read by nobody — while the
API accepted `notes` identically on both decisions and the gate prompt invited
them.

That forced a false choice. The correct review of run 9's design 6 was
"approve, and fix these three one-liners while you implement". The options were
"approve and say nothing that matters" or "reject and spend another architect
round on three lines". Taking the former cost an implementer attempt
rediscovering, through the compiler, a defect already written down.

Nothing would have failed if this regressed, which is why these exist.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType, SpecStatus

CONDITIONS = ("1. SeededRNG is a `final class` with a `mutating func` — "
              "`mutating` is not valid on a class method.\n"
              "2. `UInt64(bitPattern: seed)` takes Int64, not Int.")


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec(db):
    s = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(s.id, SpecStatus.EXECUTING)
    return db.get_spec(s.id)


def _gate(db, spec, gate_type, decision, notes):
    g = db.create_gate(spec_id=spec.id, task_id=None, gate_type=gate_type,
                       prompt_md="review me")
    db.respond_to_gate(g.id, decision, notes=notes)
    return g


# ── the lookup ───────────────────────────────────────────────────────────────

def test_approved_gate_notes_are_returned(db, spec):
    _gate(db, spec, GateType.DESIGN_APPROVAL, "approved", CONDITIONS)
    got = d._approved_gate_conditions(db, spec.id, GateType.DESIGN_APPROVAL)
    assert got is not None
    assert "SeededRNG" in got


def test_rejected_gate_notes_are_not_treated_as_conditions(db, spec):
    """A rejection already has its own channel; reading it here too would
    double-render it."""
    _gate(db, spec, GateType.DESIGN_APPROVAL, "rejected", CONDITIONS)
    assert d._approved_gate_conditions(
        db, spec.id, GateType.DESIGN_APPROVAL) is None


def test_approved_with_no_notes_is_none(db, spec):
    _gate(db, spec, GateType.DESIGN_APPROVAL, "approved", "")
    assert d._approved_gate_conditions(
        db, spec.id, GateType.DESIGN_APPROVAL) is None


def test_whitespace_only_notes_are_none(db, spec):
    _gate(db, spec, GateType.DESIGN_APPROVAL, "approved", "   \n\n  ")
    assert d._approved_gate_conditions(
        db, spec.id, GateType.DESIGN_APPROVAL) is None


def test_gate_types_do_not_leak_into_each_other(db, spec):
    _gate(db, spec, GateType.PLAN_APPROVAL, "approved", "plan conditions here")
    assert d._approved_gate_conditions(
        db, spec.id, GateType.DESIGN_APPROVAL) is None
    assert "plan conditions" in d._approved_gate_conditions(
        db, spec.id, GateType.PLAN_APPROVAL)


# ── the prompts ──────────────────────────────────────────────────────────────

def test_implementer_prompt_carries_the_conditions():
    text = executor.build_implementer_message(
        "# spec", "# design", approval_conditions=CONDITIONS)[-1]["content"]
    assert "SeededRNG" in text
    assert "UInt64(bitPattern: seed)" in text


def test_implementer_prompt_does_not_call_an_approval_a_rejection():
    """The design was approved. Telling the model otherwise invites it to
    redesign, which is the opposite of what a condition asks for."""
    text = executor.build_implementer_message(
        "# spec", "# design", approval_conditions=CONDITIONS)[-1]["content"]
    assert "approved" in text.lower()
    assert "rejected" not in text.lower()


def test_conditions_are_stated_at_spec_authority():
    text = executor.build_implementer_message(
        "# spec", "# design", approval_conditions=CONDITIONS)[-1]["content"]
    assert "HARD REQUIREMENTS" in text


def test_architect_prompt_carries_plan_conditions():
    text = executor.build_architect_message(
        "# spec", approval_conditions="Use the existing Field enum.")[-1]["content"]
    assert "Use the existing Field enum." in text
    assert "approved" in text.lower()


def test_manifest_and_per_file_prompts_carry_them_too():
    """Manifest mode is where the code is actually written, and a condition can
    change the file SET as well as file contents."""
    manifest = executor.build_manifest_message(
        "# spec", "# design", None, approval_conditions=CONDITIONS)[-1]["content"]
    assert "SeededRNG" in manifest

    entry = executor.ManifestEntry(path="A.swift", purpose="thing", exports="")
    per_file = executor.build_per_file_message(
        "# spec", "# design", [entry], entry, "", None, None,
        approval_conditions=CONDITIONS)[-1]["content"]
    assert "SeededRNG" in per_file


@pytest.mark.parametrize("empty", [None, "", "   \n "])
def test_no_conditions_leaves_every_prompt_byte_identical(empty):
    """The whole feature must be invisible when nobody attached notes."""
    base_impl = executor.build_implementer_message("# spec", "# design")
    base_arch = executor.build_architect_message("# spec")
    assert executor.build_implementer_message(
        "# spec", "# design", approval_conditions=empty) == base_impl
    assert executor.build_architect_message(
        "# spec", approval_conditions=empty) == base_arch


# ── end to end ───────────────────────────────────────────────────────────────

def test_approved_design_conditions_reach_the_implementer_call(db, spec):
    """The defect exactly as filed: approve with conditions, and check they are
    in the bytes the implementer is actually sent."""
    _gate(db, spec, GateType.DESIGN_APPROVAL, "approved", CONDITIONS)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)

    seen = {}

    def _capture(role, messages, **kw):
        seen["text"] = messages[-1]["content"]
        return "<<<FILE: A.swift>>>\nstruct A {}\n<<<END_FILE>>>"

    with mock.patch.object(d, "call_agent", side_effect=_capture), \
         mock.patch.object(d, "_fetch_existing_files_for_spec", return_value=[]), \
         mock.patch.object(d, "_fetch_protected_files_for_spec", return_value=[]):
        d._generate_implementation(db, spec, task, spec_dir, "# spec",
                                   "# design", None, [], None)

    assert "SeededRNG" in seen["text"]
    assert "UInt64(bitPattern: seed)" in seen["text"]
