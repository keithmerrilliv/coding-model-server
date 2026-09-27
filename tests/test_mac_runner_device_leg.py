"""DEV-850: the runner's device leg.

An `on_device` request runs xcodebuild_test on the physical device attached to
the Mac, unsandboxed, with Metal API validation on. It is refused, without
running anything, unless the framework is xcodebuild_test, the runner is opted
in, and discovery finds hardware. Every refusal, and every run the device
itself stopped, heads its output with `[device-unavailable] ` so the pipeline
can tell "the leg never happened" from "the code failed on the device".

Subprocess and device discovery are faked, as in test_mac_runner_server.py;
workspace.py still drives real git.
"""
import subprocess
import types

import pytest

from mac_runner import environment, server
from mac_runner.config import Config

UDID = "00008112-001A2B3C0123456E"
DEVICE = ("Keith's Vision Pro", "visionOS", UDID)


@pytest.fixture
def repo(tmp_path):
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
def client(tmp_path, repo, runner_client, monkeypatch):
    # The production defaults for containment: sandbox and VM both on, so a
    # device run proves it leaves both behind rather than never meeting them.
    c = runner_client(
        repo, WORKTREE_ROOT=tmp_path / "wt", DERIVED_DATA=tmp_path / "dd",
        SANDBOX=True, VM=True, DEVICE_TESTS=True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    monkeypatch.setattr(server.vm, "vm_available", lambda: None)
    monkeypatch.setattr(environment, "find_signing_identity",
                        lambda prefer_hash=True: ("Apple Development", "TEAM123456"))
    return c


def _attached(monkeypatch, device=DEVICE):
    monkeypatch.setattr(environment, "find_attached_device",
                        lambda destination: device)


def _fake_subprocess(monkeypatch, run):
    monkeypatch.setattr(
        server, "subprocess",
        types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )


def _record(monkeypatch, stdout="** TEST SUCCEEDED **", returncode=0):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if "-resolvePackageDependencies" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="resolved", stderr="")
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    _fake_subprocess(monkeypatch, fake_run)
    return calls


def _no_run(monkeypatch):
    _fake_subprocess(monkeypatch, lambda cmd, **kw: pytest.fail(
        f"a refused device request must not run anything, got {cmd}"))
    monkeypatch.setattr(server.vm, "run_tests_in_vm", lambda *a, **k: pytest.fail(
        "a refused device request must not reach the VM"))


def _post(client, **extra):
    body = {"spec_id": "s1", "repo": "proj", "framework": "xcodebuild_test",
            "scheme": "Demo", "destination": "platform=visionOS"}
    body.update(extra)
    resp = client.post("/v1/run_tests", headers={"X-Runner-Key": "test-key"},
                       json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── refusals: nothing runs, the marker heads the output ─────────────────────

def test_device_request_for_another_framework_is_refused(client, monkeypatch):
    _no_run(monkeypatch)
    _attached(monkeypatch)
    body = _post(client, framework="swift_test", on_device=True)
    assert body["passed"] is False
    assert body["output"].startswith("[device-unavailable] ")
    assert "xcodebuild_test" in body["output"]
    assert body["on_device"] is True


def test_device_request_is_refused_when_the_runner_is_not_opted_in(
        client, monkeypatch):
    monkeypatch.setattr(Config, "DEVICE_TESTS", False)
    _no_run(monkeypatch)
    _attached(monkeypatch)
    body = _post(client, on_device=True)
    assert body["passed"] is False
    assert body["output"].startswith("[device-unavailable] ")
    assert "CODING_MODEL_RUNNER_DEVICE_TESTS" in body["output"]


def test_device_request_is_refused_with_no_device_attached(client, monkeypatch):
    """Without hardware the destination stays platform-only, and xcodebuild
    would quietly test a simulator and call it the device leg."""
    _no_run(monkeypatch)
    _attached(monkeypatch, device=None)
    body = _post(client, on_device=True)
    assert body["passed"] is False
    assert body["output"].startswith("[device-unavailable] ")
    assert "no physical device" in body["output"]


# ── the run itself ───────────────────────────────────────────────────────────

def test_device_run_is_unsandboxed_off_the_vm_with_metal_validation(
        client, monkeypatch, caplog):
    _attached(monkeypatch)
    monkeypatch.setattr(server.vm, "run_tests_in_vm", lambda *a, **k: pytest.fail(
        "the VM cannot see a USB device; a device run must stay on the host"))
    calls = _record(monkeypatch)

    with caplog.at_level("WARNING", logger="mac_runner.server"):
        body = _post(client, on_device=True)

    assert body["passed"] is True
    assert body["on_device"] is True
    build, kw = calls[-1]
    assert build[0] == "xcodebuild", "app-hosted XCTest cannot run under sandbox-exec"
    assert server.SANDBOX_EXEC not in build
    assert "TEST_RUNNER_MTL_DEBUG_LAYER=1" in build
    assert kw["env"]["TEST_RUNNER_MTL_DEBUG_LAYER"] == "1"
    assert build[build.index("-destination") + 1] == f"platform=visionOS,id={UDID}"
    assert "UNSANDBOXED device run" in caplog.text
    assert "Keith's Vision Pro" in caplog.text


def test_explicit_device_id_is_honoured_without_discovery(client, monkeypatch):
    monkeypatch.setattr(environment, "find_attached_device", lambda d: pytest.fail(
        "an id= destination is already hardware; nothing to discover"))
    calls = _record(monkeypatch)
    body = _post(client, on_device=True, destination=f"platform=visionOS,id={UDID}")
    assert body["passed"] is True
    build, _ = calls[-1]
    assert build[build.index("-destination") + 1] == f"platform=visionOS,id={UDID}"


@pytest.mark.parametrize("line", [
    "xcodebuild: error: Unable to find a destination matching the provided "
    "destination specifier",
    "The device is locked. Unlock it to continue.",
    "Keith's Vision Pro is passcode protected",
    "Keith's Vision Pro could not be, unlocked",
    "Keith's Vision Pro is not available because it is unpaired",
    "Error: the device is not connected",
])
def test_a_locked_or_unreachable_device_is_marked(client, monkeypatch, line):
    _attached(monkeypatch)
    _record(monkeypatch, stdout=f"Build succeeded\n{line}\n", returncode=70)
    body = _post(client, on_device=True)
    assert body["passed"] is False
    assert body["output"].startswith("[device-unavailable] ")
    assert line.strip() in body["output"].splitlines()[0]


@pytest.mark.parametrize("stdout", [
    "Test Case '-[DemoTests testSum]' failed (0.01 seconds).\n** TEST FAILED **",
    # A Metal validation abort is exactly what the leg exists to catch.
    "-[MTLDebugRenderCommandEncoder validateCommonDrawErrors:]:5780: failed "
    "assertion `Draw Errors Validation'\nTest crashed with signal abrt.\n"
    "** TEST FAILED **",
])
def test_a_real_failure_on_the_device_is_not_marked(client, monkeypatch, stdout):
    _attached(monkeypatch)
    _record(monkeypatch, stdout=stdout, returncode=65)
    body = _post(client, on_device=True)
    assert body["passed"] is False
    assert not body["output"].startswith("[device-unavailable]")


# ── a request without on_device is untouched ────────────────────────────────

def test_ordinary_request_still_goes_to_the_vm(client, monkeypatch):
    _attached(monkeypatch)
    seen = {}

    def fake_vm_run(wt, resolve_cmd, cmd, **kw):
        seen["cmd"] = cmd
        return 0, "guest tests ok"

    monkeypatch.setattr(server.vm, "run_tests_in_vm", fake_vm_run)
    _fake_subprocess(monkeypatch, lambda cmd, **kw: pytest.fail(
        f"VM mode must not execute anything on the host, got {cmd}"))
    body = _post(client, destination="platform=macOS")
    assert body["passed"] is True
    assert body["on_device"] is False
    assert "TEST_RUNNER_MTL_DEBUG_LAYER=1" not in seen["cmd"]
    assert "CODE_SIGN_IDENTITY=-" in seen["cmd"]


def test_ordinary_host_request_gets_no_device_extras(client, monkeypatch):
    """VM off: the pre-DEV-850 host path, with its argv and kwargs unchanged."""
    monkeypatch.setattr(Config, "VM", False)
    _attached(monkeypatch, device=None)
    calls = _record(monkeypatch, stdout="** TEST FAILED **\nThe device is locked.",
                    returncode=65)
    body = _post(client, destination="platform=macOS")
    build, kw = calls[-1]
    assert "TEST_RUNNER_MTL_DEBUG_LAYER=1" not in build
    assert "env" not in kw
    assert not body["output"].startswith("[device-unavailable]"), (
        "the marker belongs to device requests only")
