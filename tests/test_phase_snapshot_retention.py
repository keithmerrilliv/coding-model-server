"""DEV-755: synthesis and its repair must leave an auditable snapshot.

Implementer attempts survive because each retry snapshots the previous one on
its way past. Synthesis and the repair round have no successor to do that for
them: synthesis overwrites the live attempt, the repair overwrites
``test_output.txt`` in place, and a rolled-back repair restores the pre-repair
files on top of its own. The two artifacts the pipeline produces LAST were the
two it kept nothing of.

That cost real evidence. Run 45's repair was rolled back by a comparison that
could not see its own diagnostics (the ANSI bug, fixed separately), and by the
time the gate was suspect the repair it discarded had already overwritten
itself. These tests pin the snapshots so the next rollback can be second-
guessed from disk instead of from inference.
"""
import json

from coding_model_autonomous.retry_policy import (
    _read_retry_attempts, _snapshot_retry, snapshot_phase,
)


def _spec(tmp_path, body="one"):
    spec_dir = tmp_path / "spec_x"
    spec_dir.mkdir()
    (spec_dir / "Thing.swift").write_text(f"// {body}\n")
    (spec_dir / "test_output.txt").write_text(f"build: {body}\n")
    sub = spec_dir / "Tests"
    sub.mkdir()
    (sub / "ThingTests.swift").write_text(f"// tests {body}\n")
    return spec_dir


def test_snapshot_phase_keeps_files_and_directories(tmp_path):
    spec_dir = _spec(tmp_path, "synthesis")

    snapshot_phase(spec_dir, "synthesis")

    snap = spec_dir / "retry_history" / "synthesis"
    assert (snap / "Thing.swift").read_text() == "// synthesis\n"
    assert (snap / "test_output.txt").read_text() == "build: synthesis\n"
    assert (snap / "Tests" / "ThingTests.swift").read_text() == "// tests synthesis\n"


def test_snapshot_survives_being_overwritten_in_place(tmp_path):
    """The repair overwrites the live files; the snapshot must not follow."""
    spec_dir = _spec(tmp_path, "synthesis")
    snapshot_phase(spec_dir, "synthesis")

    # The repair round, writing over synthesis's work.
    (spec_dir / "Thing.swift").write_text("// repair\n")
    (spec_dir / "test_output.txt").write_text("build: repair\n")

    snap = spec_dir / "retry_history" / "synthesis"
    assert snap.joinpath("Thing.swift").read_text() == "// synthesis\n", (
        "the synthesis snapshot tracked a later write instead of holding still")
    assert snap.joinpath("test_output.txt").read_text() == "build: synthesis\n"


def test_rolled_back_repair_is_still_on_disk(tmp_path):
    """The whole point: a repair nobody kept is still a repair somebody can read.

    Models the real sequence — snapshot the repair, then restore the
    pre-repair files over the live tree — and asserts both phases survive it.
    """
    spec_dir = _spec(tmp_path, "synthesis")
    snapshot_phase(spec_dir, "synthesis")

    pre_repair = (spec_dir / "Thing.swift").read_text()
    (spec_dir / "Thing.swift").write_text("// the repair's attempt\n")
    snapshot_phase(spec_dir, "synthesis_repair")
    (spec_dir / "Thing.swift").write_text(pre_repair)  # rollback

    hist = spec_dir / "retry_history"
    assert (spec_dir / "Thing.swift").read_text() == "// synthesis\n"
    assert (hist / "synthesis" / "Thing.swift").read_text() == "// synthesis\n"
    assert (hist / "synthesis_repair" / "Thing.swift").read_text() == (
        "// the repair's attempt\n"), "the discarded repair left no evidence"


def test_named_snapshots_are_not_read_back_as_attempts(tmp_path):
    """They live in retry_history/ but they are not retries.

    _read_retry_attempts builds the synthesis corpus from retry_history. If it
    counted these, synthesis would be fed its own previous output as though a
    sixth implementer had written it, and the attempt indices would shift.
    """
    spec_dir = _spec(tmp_path, "attempt")
    _snapshot_retry(spec_dir, retry_index=0)
    snapshot_phase(spec_dir, "synthesis")
    snapshot_phase(spec_dir, "synthesis_repair")

    attempts = _read_retry_attempts(spec_dir)

    # retry_0 plus the live tree; the two named snapshots are not attempts.
    assert [a["retry"] for a in attempts] == [0, 1], (
        f"named phase snapshots leaked into the corpus: {attempts}")


def test_snapshot_phase_is_idempotent(tmp_path):
    """A second call must refresh, not raise on the existing directory."""
    spec_dir = _spec(tmp_path, "first")
    snapshot_phase(spec_dir, "synthesis")
    (spec_dir / "Thing.swift").write_text("// second\n")
    snapshot_phase(spec_dir, "synthesis")

    snap = spec_dir / "retry_history" / "synthesis"
    assert snap.joinpath("Thing.swift").read_text() == "// second\n"


def test_snapshot_phase_does_not_recurse_into_retry_history(tmp_path):
    """Copying retry_history into itself would square the run's disk use."""
    spec_dir = _spec(tmp_path, "attempt")
    _snapshot_retry(spec_dir, retry_index=0)

    snapshot_phase(spec_dir, "synthesis")

    snap = spec_dir / "retry_history" / "synthesis"
    assert not (snap / "retry_history").exists()
    assert not (snap / "retry_0").exists()


def test_repair_verdict_record_is_json_and_names_the_rollback(tmp_path):
    """The shape orchestrator_daemon writes beside the repair snapshot.

    The files say what the repair did; only this says what the gate decided,
    and on a rollback the decision is the thing under suspicion.
    """
    spec_dir = _spec(tmp_path, "repair")
    snapshot_phase(spec_dir, "synthesis_repair")
    snap = spec_dir / "retry_history" / "synthesis_repair"
    snap.joinpath("repair_verdict.json").write_text(json.dumps({
        "repair_passed": False, "improved": False, "poisoned": True,
        "rolled_back": True, "pre_repair_diagnostics": 6,
        "post_repair_diagnostics": 36, "new_diagnostic_classes": ["x"],
        "protected_files": ["Scaffold.swift"],
    }, indent=2))

    verdict = json.loads(snap.joinpath("repair_verdict.json").read_text())
    assert verdict["rolled_back"] is (not verdict["improved"])
    assert verdict["post_repair_diagnostics"] > verdict["pre_repair_diagnostics"]


def test_the_daemon_actually_calls_both_snapshots():
    """A source-level check, deliberately.

    The tests above prove snapshot_phase works; none of them prove the daemon
    calls it, and the existing repair-tail tests drive a *mirror* of the
    production tail rather than the tail itself — so they would pass whether
    the wiring existed or not. Parsing for the two calls is the cheap way to
    stop the helper being quietly orphaned, which is the exact failure this
    ticket is about: code whose absence leaves no trace.
    """
    import ast
    import inspect

    from coding_model_server import orchestrator_daemon as od

    tree = ast.parse(inspect.getsource(od))
    labels = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "snapshot_phase"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert labels == {"synthesis", "synthesis_repair"}, (
        f"orchestrator_daemon snapshots {sorted(labels)}; both phases must be "
        "captured or a rolled-back repair is again unauditable")
