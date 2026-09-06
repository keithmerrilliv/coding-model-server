"""DEV-626: self-target pytest specs get an importable package in the sandbox.

Run 20 (spec_22192991): every implementer attempt — including the one whose
artifacts later passed 50/50 in a worktree — redded at collection with
`ModuleNotFoundError: No module named 'coding_model_autonomous'`, because the
bwrap sandbox binds only the venv and the spec workspace. The fix materializes
a merged repo+workspace tree inside spec_dir and puts it on PYTHONPATH; the
workspace copy must shadow the repo copy (tested-vs-shipped in miniature).
"""
import subprocess
from unittest import mock

import pytest

import coding_model_autonomous.test_runner as tr

SELF = tr._SERVER_REPO_ROOT.name


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec_test"
    d.mkdir()
    return d


# ── materialization ──────────────────────────────────────────────────────────

def test_foreign_repo_gets_no_overlay(spec_dir):
    assert tr._materialize_local_repo_overlay(spec_dir, "centipede") is None
    assert not (spec_dir / tr._REPO_OVERLAY_DIR).exists()


def test_absent_repo_key_gets_no_overlay(spec_dir):
    assert tr._materialize_local_repo_overlay(spec_dir, None) is None


def test_self_repo_overlay_contains_the_package(spec_dir):
    overlay_src = tr._materialize_local_repo_overlay(spec_dir, SELF)
    assert overlay_src is not None
    assert (overlay_src / "coding_model_autonomous" / "test_runner.py").is_file()
    assert (overlay_src / "coding_model_server").is_dir()


def test_workspace_file_shadows_repo_file(spec_dir):
    """The ordering requirement from the ticket: workspace wins, or the
    pre-gate check tests shipped code instead of the candidate."""
    ws = spec_dir / "src" / "coding_model_autonomous"
    ws.mkdir(parents=True)
    (ws / "executor.py").write_text("WORKSPACE-CANDIDATE\n")
    overlay_src = tr._materialize_local_repo_overlay(spec_dir, SELF)
    assert (overlay_src / "coding_model_autonomous" / "executor.py").read_text() \
        == "WORKSPACE-CANDIDATE\n"
    # A module the workspace did NOT edit keeps the repo version.
    repo_copy = (overlay_src / "coding_model_autonomous" / "models.py").read_text()
    assert "WORKSPACE-CANDIDATE" not in repo_copy and repo_copy


def test_workspace_only_file_is_present(spec_dir):
    ws = spec_dir / "src" / "coding_model_autonomous"
    ws.mkdir(parents=True)
    (ws / "brand_new_module.py").write_text("NEW = 1\n")
    overlay_src = tr._materialize_local_repo_overlay(spec_dir, SELF)
    assert (overlay_src / "coding_model_autonomous" / "brand_new_module.py").is_file()


def test_overlay_is_rebuilt_fresh_each_run(spec_dir):
    overlay_src = tr._materialize_local_repo_overlay(spec_dir, SELF)
    stale = overlay_src / "coding_model_autonomous" / "stale_leftover.py"
    stale.write_text("STALE = 1\n")
    tr._materialize_local_repo_overlay(spec_dir, SELF)
    assert not stale.exists()


def test_pycache_is_not_copied(spec_dir):
    overlay_src = tr._materialize_local_repo_overlay(spec_dir, SELF)
    assert not list(overlay_src.rglob("__pycache__"))
    assert not list(overlay_src.rglob("*.pyc"))


def test_overlay_dir_is_skipped_by_patch_collection():
    """The overlay must never ride along in a delivery patch set."""
    assert tr._REPO_OVERLAY_DIR in tr._SPEC_SKIP_PATTERNS


# ── wiring through _run_local_tests ──────────────────────────────────────────

def _capture_confined(captured):
    def fake(raw_cmd, spec_dir, timeout, **kwargs):
        captured["raw_cmd"] = raw_cmd
        captured["kwargs"] = kwargs
        return True, "ok"
    return fake


def test_pytest_self_target_sets_pythonpath_and_ignore(spec_dir):
    captured = {}
    with mock.patch.object(tr, "_run_confined", _capture_confined(captured)):
        tr._run_local_tests(spec_dir, "pytest", 60, repo=SELF)
    overlay_src = spec_dir / tr._REPO_OVERLAY_DIR / "src"
    assert captured["kwargs"]["extra_env"] == {"PYTHONPATH": str(overlay_src)}
    cmd = captured["raw_cmd"]
    assert str(spec_dir / tr._REPO_OVERLAY_DIR) in cmd[cmd.index("--ignore") :]


def test_pytest_without_repo_is_unchanged(spec_dir):
    captured = {}
    with mock.patch.object(tr, "_run_confined", _capture_confined(captured)):
        tr._run_local_tests(spec_dir, "pytest", 60)
    assert captured["kwargs"]["extra_env"] is None
    assert not (spec_dir / tr._REPO_OVERLAY_DIR).exists()


def test_run_tests_threads_repo_to_local_runner(spec_dir):
    with mock.patch.object(tr, "_run_local_tests",
                           return_value=(True, "ok")) as run_local:
        tr.run_tests(spec_dir, framework="pytest", timeout=5,
                     repo=SELF, base_ref="main")
    assert run_local.call_args.kwargs["repo"] == SELF


# ── extra_env delivery in both confinement modes ─────────────────────────────

class _FakeProc:
    pid = 4242
    returncode = 0

    def communicate(self, timeout=None):
        return "ok", ""


def test_sandbox_mode_translates_extra_env_to_setenv(spec_dir, monkeypatch):
    monkeypatch.delenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", raising=False)
    monkeypatch.setattr(tr, "_sandbox_available", lambda: True)
    monkeypatch.setattr(tr.seccomp_filter, "build_seccomp_bpf_fd", lambda: None)
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tr._run_confined(["true"], spec_dir, 5, what="tests",
                     extra_env={"PYTHONPATH": "/overlay/src"})
    cmd = seen["cmd"]
    idx = cmd.index("/overlay/src")
    assert cmd[idx - 2 : idx] == ["--setenv", "PYTHONPATH"]
    assert idx < cmd.index("--")  # a bwrap arg, not part of the test command


def test_unsandboxed_mode_merges_extra_env(spec_dir, monkeypatch):
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "1")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tr._run_confined(["true"], spec_dir, 5, what="tests",
                     extra_env={"PYTHONPATH": "/overlay/src"})
    assert seen["env"]["PYTHONPATH"] == "/overlay/src"
    assert "PATH" in seen["env"]  # merged over os.environ, not replacing it
