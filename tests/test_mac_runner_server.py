"""The mac-runner's /v1/run_tests endpoint can actually build and launch a command.

Two regressions, both of which made every Swift/Xcode dispatch fail opaquely:

  1. `build_cmd(req.framework, ..., **opts)` was called with opts taken from
     `req.model_dump()`, which also emits a `framework` key — so every request
     raised `TypeError: build_cmd() got multiple values for argument 'framework'`
     and 500ed before a single test could run.
  2. A toolchain missing from PATH escaped as an uncaught `FileNotFoundError`,
     surfacing to the orchestrator as `HTTP 500: Internal Server Error` with
     nothing actionable in it.

The command is never really executed: `subprocess` is swapped out in the server
module's namespace only, so `workspace.py` still drives real git. That keeps
these runnable on Linux, where swift/xcodebuild don't exist.
"""
import subprocess
import types

import pytest
from fastapi.testclient import TestClient

from mac_runner import server
from mac_runner.config import Config


@pytest.fixture
def repo(tmp_path):
    """A real git repo — worktree() shells out to git for real."""
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    return path


@pytest.fixture
def client(tmp_path, repo, monkeypatch):
    repos_file = tmp_path / "repos.yml"
    repos_file.write_text(f"repos:\n  proj:\n    path: {repo}\n")
    monkeypatch.setattr(Config, "REPOS_FILE", repos_file)
    monkeypatch.setattr(Config, "API_KEY", "test-key")
    monkeypatch.setattr(Config, "WORKTREE_ROOT", tmp_path / "wt")
    monkeypatch.setattr(Config, "DERIVED_DATA", tmp_path / "dd")
    # Off by default so command assertions stay platform-independent; the
    # DEV-126 sandbox tests opt in explicitly.
    monkeypatch.setattr(Config, "SANDBOX", False)
    return TestClient(server.app)


def _fake_subprocess(monkeypatch, run):
    """Replace `subprocess` in the server module only, leaving workspace.py's real."""
    monkeypatch.setattr(
        server, "subprocess",
        types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )


def test_swift_test_builds_and_launches_the_command(client, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)

    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["passed"] is True
    # The framework arg reached build_cmd exactly once, so the command got built.
    assert seen["cmd"] == ["swift", "test", "--parallel"]


def test_filter_is_forwarded_to_swift_test(client, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)

    client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test",
              "filter": "MyTests"},
    )

    assert seen["cmd"] == ["swift", "test", "--parallel", "--filter", "MyTests"]


def test_missing_toolchain_reports_cleanly_instead_of_500(client, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "swift")

    _fake_subprocess(monkeypatch, fake_run)

    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is False
    assert "not found on the runner's PATH" in body["output"]


def test_unknown_repo_is_rejected(client):
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "nope", "framework": "swift_test"},
    )
    assert resp.status_code == 400
    assert "unknown repo" in resp.text


def test_bad_key_is_rejected(client):
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "wrong"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test"},
    )
    assert resp.status_code == 401


# ── DEV-126: LLM-authored builds run confined ────────────────────────────────

def _capture_cmd(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    return seen


def _post_swift_test(client):
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test"},
    )
    assert resp.status_code == 200, resp.text


def test_sandbox_wraps_the_command_when_available(client, monkeypatch):
    seen = _capture_cmd(monkeypatch)
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)

    _post_swift_test(client)

    cmd = seen["cmd"]
    assert cmd[0] == server.SANDBOX_EXEC
    assert cmd[1:3] == ["-f", str(Config.SANDBOX_PROFILE)]
    assert cmd[-3:] == ["swift", "test", "--parallel"]
    joined = " ".join(cmd)
    assert "HOME=" in joined
    assert "WORKTREE=" in joined
    assert "DERIVED_DATA=" in joined


def test_sandbox_missing_binary_falls_back_unwrapped(client, monkeypatch):
    # Keeps the runner alive on hosts without sandbox-exec; the server logs
    # a loud warning instead of silently pretending to be confined.
    seen = _capture_cmd(monkeypatch)
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: False)

    _post_swift_test(client)

    assert seen["cmd"] == ["swift", "test", "--parallel"]


def test_sandbox_disabled_runs_unwrapped(client, monkeypatch):
    seen = _capture_cmd(monkeypatch)
    monkeypatch.setattr(Config, "SANDBOX", False)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)

    _post_swift_test(client)

    assert seen["cmd"] == ["swift", "test", "--parallel"]


def test_shipped_profile_denies_credentials_and_home_writes():
    from pathlib import Path

    profile = Path(server.__file__).parent / "sandbox.sb"
    text = profile.read_text()
    # $HOME is read-only by default...
    assert '(deny file-write* (subpath (param "HOME")))' in text
    # ...except the build's own directories, passed as parameters.
    assert '(subpath (param "WORKTREE"))' in text
    assert '(subpath (param "DERIVED_DATA"))' in text
    # Credential material is unreadable, including the runner's own config.
    assert '/.ssh' in text
    assert "Keychains" in text
    assert ".config/coding-model-runner" in text
