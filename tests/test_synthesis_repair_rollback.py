"""The synthesis repair must not commit a regression (DEV-541).

Run 8 went 3 diagnostics → 14 in the repair round and the 14 were what the spec
died with. The repair is the last operation before failure, so whatever it
leaves on disk is what the run is judged on.
"""
from pathlib import Path

import pytest

from coding_model_autonomous import executor
from coding_model_server import orchestrator_daemon as od


def _diag(path: str, line: int, msg: str) -> str:
    return f"/worktrees/abc/{path}:{line}:12: error: {msg}\n"


PRE = (
    "Building for debugging...\n"
    + _diag("Sources/World.swift", 116, "cannot find 'lc' in scope")
    + _diag("Sources/World.swift", 116, "cannot find 'tc' in scope")
    + _diag("Sources/SeededRNG.swift", 11,
            "cannot convert value of type 'Int' to expected argument type 'Int64'")
)

# The shape run 8 actually produced: the cited defect fixed, ten new ones.
WORSE = (
    "Building for debugging...\n"
    + "".join(
        _diag("Sources/CentipedeChain.swift", n, "cannot find type 'UUID' in scope")
        for n in (5, 13)
    )
    + "".join(
        _diag("Sources/HitResult.swift", n, "cannot find type 'UUID' in scope")
        for n in (15, 21)
    )
    + _diag("Sources/SeededRNG.swift", 11,
            "cannot convert value of type 'Int' to expected argument type 'Int64'")
)

BETTER = (
    "Building for debugging...\n"
    + _diag("Sources/SeededRNG.swift", 11,
            "cannot convert value of type 'Int' to expected argument type 'Int64'")
)


def test_counting_occurrences_not_classes_is_what_catches_run_8():
    """The whole guard turns on this distinction.

    WORSE carries more diagnostics than PRE but *fewer distinct messages*,
    because its UUID errors all share one message — so a set comparison scores
    it as an improvement and keeps a regression.

    Run 8's own output happens to be caught either way (27 diagnostics from 6
    distinct messages, both above the pre-repair 3). This fixture is the shape
    that is not: a dropped import whose errors all repeat one message is the
    common case, and it is why the guard counts occurrences.
    """
    assert len(od._attributed_diagnostics(PRE)) == 3
    assert len(od._attributed_diagnostics(WORSE)) == 5   # more diagnostics
    assert len(od._diagnostic_messages(WORSE)) == 2      # ...but fewer classes
    assert len(od._diagnostic_messages(PRE)) == 3
    assert len(od._attributed_diagnostics(BETTER)) == 1


def test_artifact_path_resolves_like_write_artifact():
    """The snapshot must resolve paths the same way the write does."""
    root = Path("/tmp/spec")
    assert executor.artifact_path(root, "a/b.swift") == Path("/tmp/spec/a/b.swift")
    assert executor.artifact_path(root, "/a/b.swift") == Path("/tmp/spec/a/b.swift")
    with pytest.raises(ValueError, match="traversal"):
        executor.artifact_path(root, "../../etc/passwd")


class _Recorder:
    """Minimal db double: captures events, ignores everything else."""

    def __init__(self):
        self.events = []

    def record_event(self, **kw):
        self.events.append(kw)

    def create_artifact(self, **kw):
        pass


def _run_repair_tail(tmp_path, monkeypatch, repair_output, files_before,
                     repair_files):
    """Drive the repair overlay + verdict with a stubbed test runner.

    Returns (passed, output, db, on-disk file contents).
    """
    for rel, content in files_before.items():
        executor._write_artifact(tmp_path, rel, content)

    calls = {"n": 0}

    def fake_guard(spec_id, spec_dir, framework, opts, *, output_label,
                   fail_log):
        calls["n"] += 1
        return False, repair_output

    monkeypatch.setattr(od, "_run_tests_with_guard", fake_guard)

    db = _Recorder()
    pre_state = {}
    for rel, _ in repair_files:
        target = executor.artifact_path(tmp_path, rel)
        pre_state[rel] = target.read_text() if target.is_file() else None
    pre_diags = od._attributed_diagnostics(PRE)

    # Mirror of the production tail; kept in this shape so the assertions below
    # describe the decision, not the plumbing around it.
    for rel, content in repair_files:
        executor._write_artifact(tmp_path, rel, content)
    passed, out = fake_guard(None, tmp_path, None, None, output_label="",
                             fail_log="")
    post_diags = od._attributed_diagnostics(out)
    improved = passed or len(post_diags) < len(pre_diags)
    if not improved:
        for rel, previous in pre_state.items():
            target = executor.artifact_path(tmp_path, rel)
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(previous)
        passed, out = False, PRE
    on_disk = {
        rel: (executor.artifact_path(tmp_path, rel).read_text()
              if executor.artifact_path(tmp_path, rel).is_file() else None)
        for rel in pre_state
    }
    return passed, out, db, on_disk


def test_regressing_repair_is_rolled_back(tmp_path, monkeypatch):
    before = {"Sources/CentipedeChain.swift": "import Foundation\nstruct C {}\n"}
    repair = [("Sources/CentipedeChain.swift", "struct C {}\n")]  # dropped import
    passed, out, _db, on_disk = _run_repair_tail(
        tmp_path, monkeypatch, WORSE, before, repair)

    assert passed is False
    assert out == PRE, "the pre-repair failure is what the caller should see"
    assert on_disk["Sources/CentipedeChain.swift"] == \
        "import Foundation\nstruct C {}\n", "the good file must be restored"


def test_improving_repair_is_kept(tmp_path, monkeypatch):
    before = {"Sources/World.swift": "let x = lc.id\n"}
    repair = [("Sources/World.swift", "if let lc = chain { _ = lc.id }\n")]
    passed, out, _db, on_disk = _run_repair_tail(
        tmp_path, monkeypatch, BETTER, before, repair)

    assert out == BETTER, "an improved build is the one worth reporting"
    assert on_disk["Sources/World.swift"] == "if let lc = chain { _ = lc.id }\n"


def test_rollback_deletes_a_file_the_repair_invented(tmp_path, monkeypatch):
    repair = [("Sources/Brand New.swift", "struct N {}\n")]
    _passed, _out, _db, on_disk = _run_repair_tail(
        tmp_path, monkeypatch, WORSE, {}, repair)
    assert on_disk["Sources/Brand New.swift"] is None


def test_equal_diagnostic_count_counts_as_no_improvement(tmp_path, monkeypatch):
    """Three different errors instead of three is churn, not progress."""
    same_count = (
        _diag("Sources/A.swift", 1, "alpha")
        + _diag("Sources/B.swift", 2, "beta")
        + _diag("Sources/C.swift", 3, "gamma")
    )
    before = {"Sources/A.swift": "original\n"}
    repair = [("Sources/A.swift", "rewritten\n")]
    passed, out, _db, on_disk = _run_repair_tail(
        tmp_path, monkeypatch, same_count, before, repair)
    assert passed is False
    assert out == PRE
    assert on_disk["Sources/A.swift"] == "original\n"


def test_fewer_diagnostics_kept_even_when_the_survivor_is_new(tmp_path,
                                                             monkeypatch):
    """12 → 1 must be kept, even though the survivor was not seen before.

    This is the case the ticket's original "any new class vetoes it" rule would
    have discarded.
    """
    only_new = _diag("Sources/Z.swift", 9, "a message never seen before")
    before = {"Sources/Z.swift": "original\n"}
    repair = [("Sources/Z.swift", "much better\n")]
    passed, out, _db, on_disk = _run_repair_tail(
        tmp_path, monkeypatch, only_new, before, repair)
    assert out == only_new, "a strictly smaller diagnostic set is progress"
    assert on_disk["Sources/Z.swift"] == "much better\n"
