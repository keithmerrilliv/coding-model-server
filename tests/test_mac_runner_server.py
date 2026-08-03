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
    # Same for VM containment (DEV-422): host-path tests must not depend on
    # tart existing; the VM tests opt in and stub the vm module.
    monkeypatch.setattr(Config, "VM", False)
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
# confined. Its real containment is the VM (DEV-422, section below); these
# tests pin the explicit CODING_MODEL_RUNNER_VM=0 fallback, where the run
# stays on the host, unwrapped, with a loud warning.

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


# ── DEV-422: VM containment for frameworks sandbox-exec cannot hold ──────────
#
# The DEV-417 decision, spike-proven in DEV-421: xcodebuild_test dispatches
# into an ephemeral tart VM instead of running unsandboxed on the host. The
# guest signs ad-hoc and builds into its own throwaway DerivedData; nothing
# of the host's enters it. CODING_MODEL_RUNNER_VM=0 is the only way back to
# the old warned host behavior, and a missing VM prerequisite fails the run
# actionably rather than silently degrading to an unconfined host exec.

def test_xcodebuild_dispatches_into_a_vm_not_onto_the_host(client, monkeypatch):
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(Config, "VM", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    monkeypatch.setattr(server.vm, "vm_available", lambda: None)
    seen = {}

    def fake_vm_run(wt, resolve_cmd, cmd, *, timeout, resolve_timeout):
        seen["resolve_cmd"] = resolve_cmd
        seen["cmd"] = cmd
        return 0, "guest tests ok"

    monkeypatch.setattr(server.vm, "run_tests_in_vm", fake_vm_run)
    _fake_subprocess(monkeypatch, lambda cmd, **kw: pytest.fail(
        f"VM mode must not execute anything on the host, got {cmd}"))

    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "xcodebuild_test",
              "scheme": "Demo"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert "guest tests ok" in body["output"]
    # Both commands target the GUEST's DerivedData — building into the host's
    # would be wrong twice over (path doesn't exist in the guest; and the
    # guest must not share host build state).
    from mac_runner import vm as vm_module
    assert vm_module.GUEST_DERIVED_DATA in seen["cmd"]
    assert "-resolvePackageDependencies" in seen["resolve_cmd"]
    assert vm_module.GUEST_DERIVED_DATA in seen["resolve_cmd"]
    # Ad-hoc signing: no identity, keychain, or password enters the guest.
    assert "CODE_SIGN_IDENTITY=-" in seen["cmd"]


def test_vm_unavailable_fails_actionably_instead_of_falling_back(
        client, monkeypatch):
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(Config, "VM", True)
    monkeypatch.setattr(
        server.vm, "vm_available",
        lambda: "tart is not installed — brew install cirruslabs/cli/tart")
    _fake_subprocess(monkeypatch, lambda cmd, **kw: pytest.fail(
        "an unavailable VM must NOT degrade to an unconfined host run"))

    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "xcodebuild_test",
              "scheme": "Demo"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is False
    assert body["exit_code"] is None
    assert "tart is not installed" in body["output"]


def test_vm_disabled_flag_warns_and_runs_on_the_host(client, monkeypatch, caplog):
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(Config, "VM", False)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    seen = _capture_cmd(monkeypatch)

    with caplog.at_level("WARNING", logger="mac_runner.server"):
        resp = client.post(
            "/v1/run_tests",
            headers={"X-Runner-Key": "test-key"},
            json={"spec_id": "s1", "repo": "proj",
                  "framework": "xcodebuild_test", "scheme": "Demo"},
        )

    assert resp.status_code == 200, resp.text
    assert "CODING_MODEL_RUNNER_VM=0" in caplog.text, (
        "the fallback must be loud — silence here reads as containment")
    assert seen["cmd"][0] == "xcodebuild", "fallback runs on the host, unwrapped"


def test_vm_mode_leaves_swift_test_on_the_sandboxed_host_path(client, monkeypatch):
    """VM containment is for the sandbox-incompatible frameworks only —
    swift_test keeps its cheaper sandbox-exec confinement."""
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(Config, "VM", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    monkeypatch.setattr(server.vm, "run_tests_in_vm", lambda *a, **k: pytest.fail(
        "swift_test sandboxes fine and must not pay the VM overhead"))
    seen = _capture_cmd(monkeypatch)

    _post_swift_test(client)

    assert seen["cmd"][0] == server.SANDBOX_EXEC


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
