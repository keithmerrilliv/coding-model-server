"""Targeted retry widens when the cited files are not where the fix is — DEV-434.

Targeted retry regenerates only the files the compiler *cited*. That is right
when the defect is local to them, and wrong when the diagnostic names the
victim rather than the cause — which is what access-control errors, missing
@testable imports and signature mismatches all look like.

On spec_ead8f7fc, `struct World` was internal while the tests used a plain
`import CentipedeCore`. Every diagnostic cited a test file, so from attempt 3
onward the retry rewrote test files exclusively and World.swift was never
revisited. Seven generations, seven Mac dispatches, same error every time.
"""
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


# ── signature ────────────────────────────────────────────────────────────────

def test_same_defect_across_dispatches_has_one_signature():
    """Worktree paths and line numbers move between attempts; the defect does not."""
    a = ("/Users/km4/Caches/worktrees/spec_x-7f8a8795/Tests/T.swift:7:17: "
         "error: cannot find 'World' in scope")
    b = ("/Users/km4/Caches/worktrees/spec_x-d14e1cfe/Tests/T.swift:12:9: "
         "error: cannot find 'World' in scope")
    assert d._failure_signature(a) == d._failure_signature(b) != ""


def test_ordering_does_not_change_the_signature():
    a = "x.swift:1:1: error: alpha\ny.swift:2:2: error: beta\n"
    b = "y.swift:9:9: error: beta\nx.swift:8:8: error: alpha\n"
    assert d._failure_signature(a) == d._failure_signature(b)


def test_different_defects_differ():
    a = "x.swift:1:1: error: cannot find 'World' in scope"
    b = "x.swift:1:1: error: cannot convert value of type 'Int'"
    assert d._failure_signature(a) != d._failure_signature(b)


def test_no_errors_gives_an_empty_signature():
    assert d._failure_signature("all good, 17 passed") == ""
    assert d._failure_signature("") == ""


# ── repeat counting ──────────────────────────────────────────────────────────

def _reject(db, spec_id, notes, prompt="## Code review: demo"):
    gate = db.create_gate(spec_id=spec_id, task_id=None,
                          gate_type=GateType.CODE_REVIEW, prompt_md=prompt)
    db.respond_to_gate(gate.id, "rejected", notes=notes)


ERR_A = "T.swift:7:17: error: cannot find 'World' in scope"
ERR_B = "S.swift:3:1: error: cannot convert value of type 'Int' to 'Int64'"


def test_counts_consecutive_identical_failures(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    _reject(db, spec.id, ERR_A)
    _reject(db, spec.id, ERR_A.replace("7:17", "9:2"))  # same defect, moved
    current = ERR_A.replace("7:17", "11:4")
    _reject(db, spec.id, current)

    assert d._consecutive_identical_failures(db, spec.id, current) == 2


def test_a_different_failure_resets_the_streak(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    _reject(db, spec.id, ERR_A)
    _reject(db, spec.id, ERR_B)          # progress — streak broken here
    current = ERR_A
    _reject(db, spec.id, current)

    assert d._consecutive_identical_failures(db, spec.id, current) == 0


def test_first_failure_has_no_prior(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    _reject(db, spec.id, ERR_A)
    assert d._consecutive_identical_failures(db, spec.id, ERR_A) == 0


def test_unparseable_notes_never_count_as_a_repeat(db):
    """Otherwise two content-free failures would widen for no reason."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    _reject(db, spec.id, "something went wrong")
    _reject(db, spec.id, "something went wrong")
    assert d._consecutive_identical_failures(db, spec.id, "something went wrong") == 0


def test_approved_gates_are_ignored(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    g = db.create_gate(spec_id=spec.id, task_id=None,
                       gate_type=GateType.CODE_REVIEW, prompt_md="## Code review")
    db.respond_to_gate(g.id, "approved", notes=ERR_A)
    _reject(db, spec.id, ERR_A)
    assert d._consecutive_identical_failures(db, spec.id, ERR_A) == 0


class _Stop(Exception):
    """Halt the run once the targeted-vs-full decision has been made."""


def _manifest_decision(db, spec, notes, *, entries=("a.swift", "b.swift", "c.swift")):
    """Drive the manifest retry branch; return ("targeted", paths) or ("full", mode).

    Widening deliberately falls through to full regeneration, which would
    otherwise reach a live call_agent — so that call is stubbed to abort.
    """
    from types import SimpleNamespace
    from unittest import mock

    manifest = [SimpleNamespace(path=p) for p in entries]
    prior_files = {p: "prior" for p in entries}
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    db.increment_task_retry(task.id)
    captured = {}

    def _fake_build(*args, **kwargs):
        captured["only"] = kwargs.get("only")
        return "RESULT"

    with mock.patch.object(d, "_load_prior_manifest_run",
                           return_value=(manifest, prior_files)), \
         mock.patch.object(d, "_build_from_manifest", side_effect=_fake_build), \
         mock.patch.object(d, "call_agent", side_effect=_Stop), \
         mock.patch.object(d, "_persist_manifest"):
        try:
            d._generate_via_manifest(
                db, db.get_spec(spec.id), db.get_task(task.id),
                db.spec_dir(spec.id), "# spec", "# design", None, [], notes)
        except _Stop:
            pass

    if "only" in captured:
        return "targeted", captured["only"]
    modes = [e.payload_json for e in db.list_recent_events(spec_id=spec.id, limit=50)]
    return "full", modes


def test_second_identical_failure_regenerates_everything(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    notes = "a.swift:1:1: error: cannot find 'World' in scope"
    _reject(db, spec.id, notes)
    _reject(db, spec.id, notes)

    kind, payloads = _manifest_decision(db, spec, notes)
    assert kind == "full", "widening must regenerate the whole manifest"
    assert any("widened_after_repeat" in str(p) for p in payloads)


def test_a_novel_failure_still_targets_only_the_cited_file(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    _reject(db, spec.id, "a.swift:1:1: error: cannot find 'World' in scope")
    notes = "b.swift:4:2: error: cannot convert value of type 'Int'"
    _reject(db, spec.id, notes)

    kind, only = _manifest_decision(db, spec, notes)
    assert kind == "targeted"
    assert only == {"b.swift"}


def test_unattributed_error_never_targets(db):
    """DEV-435: with no file:line the cited set is whatever cascaded."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    notes = ("error: emit-module command failed with exit code 1\n"
             "a.swift:1:1: error: cannot find 'World' in scope")
    _reject(db, spec.id, notes)

    kind, payloads = _manifest_decision(db, spec, notes)
    assert kind == "full"
    assert any("widened_unattributed" in str(p) for p in payloads)


def test_the_run_that_motivated_this_would_have_widened(db):
    """spec_ead8f7fc: attempts 3-7 all reported the same unresolved symbol."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    for line in (7, 12, 4, 9):
        _reject(db, spec.id, ERR_A.replace("7:17", f"{line}:1"))
    current = ERR_A.replace("7:17", "22:3")
    _reject(db, spec.id, current)

    repeats = d._consecutive_identical_failures(db, spec.id, current)
    assert repeats == 4
    assert repeats >= d.TARGETED_RETRY_MAX_REPEATS  # would widen
