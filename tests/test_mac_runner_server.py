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
# Amended by DEV-403: the xcodebuild build/test step is no longer confined
# either (see the DEV-403 section below), but the split and the anti-nesting
# flags stay — they are what lets the command run identically whether or not
# a future containment mechanism wraps it again.

def test_package_resolution_runs_outside_the_sandbox(client, monkeypatch):
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
    assert "-disableAutomaticPackageResolution" in build, (
        "the build step must not re-resolve — with a sandbox that nests, "
        "and without one it wastes the pre-step")
    # The toolchain sandboxes itself in TWO places. Macro plugins are the
    # second: swift-frontend runs them under sandbox-exec, so any dependency
    # using a macro nests and fails with "swift-plugin-server produced
    # malformed response". Found only by running a real build (DEV-294).
    macro_flag = [a for a in build if a.startswith("OTHER_SWIFT_FLAGS=")]
    assert macro_flag, "macro plugins nest unless swift-frontend is told not to sandbox"
    assert "-disable-sandbox" in macro_flag[0]
    assert "$(inherited)" in macro_flag[0], (
        "must not clobber a project's own OTHER_SWIFT_FLAGS")


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


# ── DEV-403 / DEV-417: app-hosted XCTest is exempt from the sandbox ──────────
#
# Verified on hardware: app-hosted XCTest hangs for the full test-launch
# timeout under sandbox-exec even with a deny-nothing (allow default) profile,
# and passes in seconds without the wrapper. The wrapper's presence is the
# differentiator — not the profile, not the host app's entitlements. So
# xcodebuild_test must never be wrapped, while every other framework stays
# confined; forcing CODING_MODEL_RUNNER_SANDBOX=0 globally was the interim
# state this scoping exists to end.

def test_xcodebuild_test_is_exempt_from_the_sandbox(client, monkeypatch):
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
    build = calls[-1]
    assert build[0] != server.SANDBOX_EXEC, (
        "app-hosted XCTest hangs under sandbox-exec regardless of profile "
        "(DEV-403) — wrapping it makes every run fail at the launch timeout")
    assert build[0] == "xcodebuild"


def test_sandbox_exemption_is_per_framework_not_global(client, monkeypatch):
    """The scoping's point: one framework opts out, the rest stay confined."""
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    for framework, extra in (("xcodebuild_test", {"scheme": "Demo"}),
                             ("swift_test", {})):
        resp = client.post(
            "/v1/run_tests",
            headers={"X-Runner-Key": "test-key"},
            json={"spec_id": "s1", "repo": "proj", "framework": framework,
                  **extra},
        )
        assert resp.status_code == 200, resp.text
    assert len(calls) == 3, f"expected resolve + xcode build + swift build, got {calls}"
    _resolve, xcode_build, swift_build = calls
    assert xcode_build[0] == "xcodebuild"
    assert "test" in xcode_build
    assert swift_build[0] == server.SANDBOX_EXEC, (
        "swift_test has no host app and sandboxes fine — exempting more than "
        "the incompatible framework forfeits containment for no reason")


# ── DEV-415 / DEV-416: unlock the signing keychain at startup ────────────────
#
# Keychains lock on every reboot. The runner is headless, so codesign's GUI
# unlock prompt can never be answered — the first signed build after a restart
# fails with errSecInternalComponent, and the xcodebuild error names a missing
# certificate rather than the locked keychain. main() therefore unlocks the
# keychain itself, using the password from the .env.

def test_startup_unlocks_the_signing_keychain(monkeypatch):
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN", "/kc/runner.keychain-db")
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN_PASSWORD", "kc-pass")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    assert server.unlock_signing_keychain() is True
    assert calls == [["security", "unlock-keychain",
                      "-p", "kc-pass", "/kc/runner.keychain-db"]]


def test_unlock_is_a_noop_without_a_configured_keychain(monkeypatch):
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN", "")
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN_PASSWORD", "")
    _fake_subprocess(monkeypatch, lambda cmd, **kw: pytest.fail(
        "no keychain configured — nothing should be executed"))
    assert server.unlock_signing_keychain() is True


def test_missing_keychain_password_warns_and_reports_failure(
        monkeypatch, caplog):
    """Keychain without password is the pre-DEV-416 trap: it works until the
    next reboot, then fails with an unrelated-looking signing error. Surface
    it at startup instead."""
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN", "/kc/runner.keychain-db")
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN_PASSWORD", "")
    _fake_subprocess(monkeypatch, lambda cmd, **kw: pytest.fail(
        "no password — security must not be invoked"))
    with caplog.at_level("WARNING", logger="mac_runner.server"):
        assert server.unlock_signing_keychain() is False
    assert "errSecInternalComponent" in caplog.text


def test_failed_unlock_is_loud_but_not_fatal(monkeypatch, caplog):
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN", "/kc/runner.keychain-db")
    monkeypatch.setattr(Config, "SIGNING_KEYCHAIN_PASSWORD", "wrong")
    _fake_subprocess(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 51, stdout="", stderr="The user name or passphrase is incorrect."))
    with caplog.at_level("ERROR", logger="mac_runner.server"):
        assert server.unlock_signing_keychain() is False
    assert "passphrase is incorrect" in caplog.text, (
        "security's stderr is the only clue the recorded password is wrong — "
        "swallowing it recreates the DEV-415 debugging séance")


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
