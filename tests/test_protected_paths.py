"""Off-limits files are restored to the base revision — DEV-427.

The runner materialises a git worktree at base_ref and then writes the patch
files over it, so a path simply not sent keeps whatever the base commit has.
That makes restoration free: no diff, no checkout, no round-trip.

On DEV-102 slice 1 all five attempts edited the two scaffold files the spec
put off-limits. Retry 2 destroyed them outright — deleting score/lives/wave
from the game state type and replacing both baseline tests while claiming in
its own file header to have preserved them verbatim. Those baseline tests are
the DEV-399 canary that proves a patch landed in the right place; an
implementer free to rewrite them makes the canary test whatever it felt like
writing.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import test_runner as tr
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult
from coding_model_autonomous.models import GateStatus, SpecStatus

SCAFFOLD = "Sources/CentipedeCore/GameState.swift"
BASELINE = "Tests/CentipedeCoreTests/GameStateTests.swift"


# ── the filter ───────────────────────────────────────────────────────────────

def _files(*paths):
    return [{"path": p, "content": "x"} for p in paths]


def test_protected_paths_are_dropped_from_the_payload():
    kept, dropped = tr._drop_protected(
        _files(SCAFFOLD, "Sources/CentipedeCore/Simulation.swift"), [SCAFFOLD])
    assert dropped == [SCAFFOLD]
    assert [k["path"] for k in kept] == ["Sources/CentipedeCore/Simulation.swift"]


def test_no_protected_list_keeps_everything():
    files = _files(SCAFFOLD, "a.swift")
    for empty in (None, [], ["", "  "]):
        kept, dropped = tr._drop_protected(files, empty)
        assert dropped == []
        assert len(kept) == len(files)


def test_leading_dot_slash_and_whitespace_are_tolerated():
    kept, dropped = tr._drop_protected(_files(SCAFFOLD), [f"  ./{SCAFFOLD}  "])
    assert dropped == [SCAFFOLD]
    assert kept == []


def test_untouched_protected_path_is_not_reported():
    """Declaring a path off-limits that nobody wrote is a no-op, not a warning."""
    kept, dropped = tr._drop_protected(_files("a.swift"), [SCAFFOLD])
    assert dropped == []
    assert len(kept) == 1


def test_dispatch_omits_protected_paths_and_says_so():
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"passed": True, "output": "Executed 2 tests"}

    def _post(url, json=None, headers=None, timeout=None):
        captured["paths"] = [f["path"] for f in json["patch_files"]]
        return _Resp()

    with mock.patch.object(tr, "MAC_RUNNER_API_KEY", "k"), \
         mock.patch.object(tr, "MAC_RUNNER_URL", "http://runner"), \
         mock.patch.object(tr, "_collect_patch_files",
                           return_value=(_files(SCAFFOLD, "Sources/S.swift"), None)), \
         mock.patch.object(tr._SESSION, "post", side_effect=_post):
        passed, output = tr._run_mac_runner_tests(
            spec_dir=mock.MagicMock(name="spec_dir"),
            framework="swift_test", timeout=60,
            repo="centipede", base_ref="main",
            protected_paths=[SCAFFOLD],
        )

    assert captured["paths"] == ["Sources/S.swift"]  # scaffold never sent
    assert passed is True
    assert "protected paths" in output
    assert SCAFFOLD in output
    assert "restored to main" in output


# ── the gate tells the reviewer ──────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _run_implementer(db, *, emitted, protected):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# demo\n")
    (spec_dir / "design.md").write_text("# design\n")

    result = ImplementerResult(files=[(p, "content") for p in emitted], raw="")
    strategy = {"framework": "swift_test", "repo": "centipede",
                "protected_paths": protected}
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan", return_value={"test_strategy": strategy}), \
         mock.patch.object(d, "_run_tests_with_guard",
                           return_value=(True, "Executed 2 tests")):
        d._run_implementer(db, db.get_spec(spec.id), db.get_task(task.id), spec_dir)

    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    return pending[0]


def test_gate_names_every_off_limits_path_touched(db):
    gate = _run_implementer(
        db, emitted=[SCAFFOLD, BASELINE, "Sources/CentipedeCore/Simulation.swift"],
        protected=[SCAFFOLD, BASELINE])
    assert "OFF-LIMITS FILES MODIFIED" in gate.prompt_md
    assert SCAFFOLD in gate.prompt_md
    assert BASELINE in gate.prompt_md
    assert "restored to the base revision" in gate.prompt_md


def test_gate_is_quiet_when_nothing_off_limits_was_touched(db):
    gate = _run_implementer(db, emitted=["Sources/CentipedeCore/Simulation.swift"],
                            protected=[SCAFFOLD, BASELINE])
    assert "OFF-LIMITS" not in gate.prompt_md


def test_no_protected_paths_configured_is_quiet(db):
    gate = _run_implementer(db, emitted=[SCAFFOLD], protected=[])
    assert "OFF-LIMITS" not in gate.prompt_md
