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
    assert seen["cmd"] == ["swift", "test", "--parallel", "--disable-sandbox"]


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

    assert seen["cmd"] == ["swift", "test", "--parallel", "--disable-sandbox",
                           "--filter", "MyTests"]


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
    assert cmd[cmd.index("swift"):] == ["swift", "test", "--parallel",
                                       "--disable-sandbox"]
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

    assert seen["cmd"] == ["swift", "test", "--parallel", "--disable-sandbox"]


def test_sandbox_disabled_runs_unwrapped(client, monkeypatch):
    seen = _capture_cmd(monkeypatch)
    monkeypatch.setattr(Config, "SANDBOX", False)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)

    _post_swift_test(client)

    assert seen["cmd"] == ["swift", "test", "--parallel", "--disable-sandbox"]


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


# ── DEV-170 / DEV-171: health disclosure and git option confusion ────────────

def test_health_does_not_enumerate_repos(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok"}, "repo names must not leak unauthenticated"


def test_repo_list_requires_auth(client):
    assert client.get("/v1/repos").status_code == 401
    ok = client.get("/v1/repos", headers={"X-Runner-Key": "test-key"})
    assert ok.status_code == 200
    assert ok.json()["repos"] == ["proj"]


def test_worktree_add_uses_a_double_dash_separator(client, monkeypatch, repo):
    # base_ref is request-controlled; as a trailing positional a value
    # starting with "-" would be parsed by git as a flag.
    from mac_runner import workspace

    seen = {}
    real_git = workspace._git

    def spy(repo_path, *args, **kwargs):
        if args and args[0] == "worktree" and args[1] == "add":
            seen["argv"] = args
        return real_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(workspace, "_git", spy)
    _fake_subprocess(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "ok", ""))

    client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test"},
    )

    argv = seen.get("argv")
    assert argv, "worktree add was never invoked"
    assert "--" in argv, "operands must be guarded by a -- separator"
    assert argv.index("--") < len(argv) - 2, "-- must precede path and base_ref"


# ── DEV-294: sandbox nesting ─────────────────────────────────────────────────
#
# The runner wraps builds in sandbox-exec (DEV-126), but SwiftPM spawns its own
# sandbox-exec to evaluate Package.swift. macOS cannot nest sandboxes, so the
# inner sandbox_apply returned EPERM and every xcodebuild against a project with
# package dependencies died in "Resolve Package Graph" before compiling.
#
# The fix splits the work, so the property worth pinning is not "resolution
# happens" but WHICH STEP IS CONFINED: resolution outside, build inside.

def test_package_resolution_runs_outside_the_sandbox_and_the_build_inside(
        client, monkeypatch):
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "xcodebuild_test",
              "scheme": "Demo"},
    )
    assert resp.status_code == 200, resp.text
    assert len(calls) == 2, f"expected resolve + build, got {calls}"

    resolve, build = calls
    assert "-resolvePackageDependencies" in resolve
    assert resolve[0] != server.SANDBOX_EXEC, (
        "resolution must NOT be sandboxed — SwiftPM sandboxes manifest "
        "evaluation itself and macOS cannot nest sandboxes")
    assert build[0] == server.SANDBOX_EXEC, (
        "the build runs the LLM-authored patch and must stay confined")
    assert "-disableAutomaticPackageResolution" in build, (
        "the sandboxed step must not re-resolve, or it nests again")


def test_swift_test_disables_swiftpms_own_sandbox(client, monkeypatch):
    """swift_test needs no pre-step: --disable-sandbox stops SwiftPM nesting,
    and our profile still confines the whole process tree."""
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "swift_test"},
    )
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1, f"swift_test needs no resolve pre-step, got {calls}"
    assert "--disable-sandbox" in calls[0]
    assert calls[0][0] == server.SANDBOX_EXEC


def test_resolution_failure_is_surfaced_not_swallowed(client, monkeypatch):
    """A failed resolve is non-fatal — the build may run from a warm cache —
    but it must appear in the output, or a build failure caused by it looks
    inexplicable."""
    monkeypatch.setattr(Config, "SANDBOX", False)

    def fake_run(cmd, **kw):
        if "-resolvePackageDependencies" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no network")
        return subprocess.CompletedProcess(cmd, 0, stdout="tests ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "xcodebuild_test",
              "scheme": "Demo"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "package resolution failed" in body["output"]
    assert "no network" in body["output"]
    assert "tests ok" in body["output"]
