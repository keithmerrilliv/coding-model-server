"""mac_runner.vm: the throwaway-VM lifecycle around a test dispatch (DEV-422).

Everything is faked at the vm module's subprocess seam — no tart, sshpass, or
network is touched, so these run on Linux CI. The load-bearing contract is
teardown: a leaked VM holds tens of GB, so `tart stop` + `tart delete` must
run on EVERY exit path, success or not.
"""
import subprocess
import types


from mac_runner import vm
from mac_runner.config import Config


class _FakeBootProc:
    """Stands in for the `tart run` Popen: alive until killed."""

    def __init__(self, calls):
        self._calls = calls
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._calls.append(["<kill tart-run>"])


def _wire(monkeypatch, behave):
    """Route vm's subprocess through *behave*(cmd) and record every command.

    behave returns a CompletedProcess (or raises); unhandled commands get a
    default rc-0 result.
    """
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        result = behave(cmd)
        if result is not None:
            return result
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        return _FakeBootProc(calls)

    monkeypatch.setattr(vm, "subprocess", types.SimpleNamespace(
        run=fake_run,
        Popen=fake_popen,
        TimeoutExpired=subprocess.TimeoutExpired,
        DEVNULL=subprocess.DEVNULL,
        STDOUT=subprocess.STDOUT,
    ))
    return calls


def _happy(cmd):
    """Default behavior: everything succeeds, the guest answers at an IP."""
    if cmd[:2] == ["tart", "ip"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="192.0.2.21\n", stderr="")
    if "xcodebuild test" in cmd[-1]:
        return subprocess.CompletedProcess(cmd, 0, stdout="guest tests ok", stderr="")
    return None


def _dispatch(tmp_path):
    return vm.run_tests_in_vm(
        tmp_path,
        ["xcodebuild", "-resolvePackageDependencies", "-scheme", "Demo"],
        ["xcodebuild", "test", "-scheme", "Demo"],
        timeout=600, resolve_timeout=300,
    )


def _teardown_calls(calls):
    return [c for c in calls if c[:2] in (["tart", "stop"], ["tart", "delete"])]


def test_success_path_runs_and_destroys_the_vm(tmp_path, monkeypatch):
    calls = _wire(monkeypatch, _happy)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code == 0
    assert "guest tests ok" in output
    stages = [c[0] for c in calls]
    assert stages.count("tart") >= 4, calls  # clone, run, ip, stop, delete
    torn = _teardown_calls(calls)
    assert [c[:2] for c in torn] == [["tart", "stop"], ["tart", "delete"]]
    # rsync went to the guest worktree, resolve ran before the test
    joined = [" ".join(c) for c in calls]
    assert any("rsync" in j and vm.GUEST_WORKTREE in j for j in joined)
    resolve_i = next(i for i, j in enumerate(joined) if "resolvePackageDependencies" in j)
    test_i = next(i for i, j in enumerate(joined) if "xcodebuild test" in j)
    assert resolve_i < test_i


def test_vm_is_destroyed_when_the_test_step_times_out(tmp_path, monkeypatch):
    def behave(cmd):
        if "xcodebuild test" in cmd[-1]:
            raise subprocess.TimeoutExpired(cmd, 600)
        return _happy(cmd)

    calls = _wire(monkeypatch, behave)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code is None
    assert "timed out" in output
    assert len(_teardown_calls(calls)) == 2, (
        "a timed-out run must still stop and delete its VM — a leak here "
        "costs tens of GB per occurrence")


def test_hung_ssh_probe_means_not_ready_yet_not_a_crash(tmp_path, monkeypatch):
    """Early in boot the guest's TCP stack answers before sshd does, so the
    readiness probe can hang and hit its own timeout. Seen on the first real
    dispatch: the TimeoutExpired escaped run_tests_in_vm entirely and the
    endpoint 500ed. A hung probe is just 'keep waiting'."""
    state = {"probes": 0}

    def behave(cmd):
        if cmd[-1] == "true":
            state["probes"] += 1
            if state["probes"] < 3:
                raise subprocess.TimeoutExpired(cmd, 15)
        return _happy(cmd)

    monkeypatch.setattr(vm.time, "sleep", lambda s: None)
    calls = _wire(monkeypatch, behave)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code == 0, output
    assert state["probes"] == 3
    assert len(_teardown_calls(calls)) == 2


def test_vm_is_destroyed_when_the_guest_never_comes_up(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "VM_BOOT_TIMEOUT", 0)
    calls = _wire(monkeypatch, _happy)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code is None
    assert "not reachable" in output
    assert len(_teardown_calls(calls)) == 2


def test_clone_failure_reports_tarts_stderr(tmp_path, monkeypatch):
    def behave(cmd):
        if cmd[:2] == ["tart", "clone"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="no such image")
        return _happy(cmd)

    _wire(monkeypatch, behave)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code is None
    assert "no such image" in output


def test_ssh_transport_failure_is_not_a_test_verdict(tmp_path, monkeypatch):
    """ssh exits 255 for its own failures; reporting that as exit_code 255
    would read as 'the tests failed' when nothing ever ran."""
    def behave(cmd):
        if "xcodebuild test" in cmd[-1]:
            return subprocess.CompletedProcess(
                cmd, 255, stdout="", stderr="Connection closed")
        return _happy(cmd)

    _wire(monkeypatch, behave)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code is None
    assert "ssh transport failed" in output


def test_failed_in_vm_resolution_is_surfaced_not_fatal(tmp_path, monkeypatch):
    """Same contract as the host pre-step (DEV-294): the build may still
    succeed, but the resolution failure must be visible in the output."""
    def behave(cmd):
        if "resolvePackageDependencies" in cmd[-1]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="no network in guest")
        return _happy(cmd)

    _wire(monkeypatch, behave)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code == 0
    assert "package resolution failed" in output
    assert "no network in guest" in output
    assert "guest tests ok" in output


def test_guest_command_keeps_inherited_literal():
    """The guest shell must hand OTHER_SWIFT_FLAGS=$(inherited) to xcodebuild
    verbatim — expanding $(inherited) as a command substitution would run
    `inherited` and clobber the setting."""
    sh = vm._guest_sh(
        ["xcodebuild", "test", "OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox"],
        vm.GUEST_WORKTREE)
    assert "'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox'" in sh
    assert sh.startswith(f"cd {vm.GUEST_WORKTREE} && ")


def test_guest_ssh_ignores_the_hosts_identities_and_config():
    """With an ssh-agent in reach, ssh offers every loaded key before the
    password and the guest cuts the connection at MaxAuthTries ("Too many
    authentication failures") — it never reaches the password. Killed a real
    deploy run, so the guest connection must be password-only and blind to
    the host's ~/.ssh/config."""
    opts = vm._SSH_OPTS
    assert "-F" in opts and opts[opts.index("-F") + 1] == "/dev/null"
    for flag in ("IdentitiesOnly=yes", "IdentityAgent=none",
                 "PubkeyAuthentication=no", "PreferredAuthentications=password"):
        assert flag in opts, f"{flag} missing — an agent key can crowd out the password"


def test_vm_available_names_the_missing_prerequisite(monkeypatch):
    monkeypatch.setattr(vm.shutil, "which", lambda name: None)
    reason = vm.vm_available()
    assert reason is not None and "tart" in reason


def test_vm_available_requires_the_image_to_be_pulled(monkeypatch):
    monkeypatch.setattr(vm.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(vm, "subprocess", types.SimpleNamespace(
        run=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="Source Name\nlocal something-else\n", stderr=""),
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    reason = vm.vm_available()
    assert reason is not None and "tart pull" in reason, (
        "clone would otherwise trigger a mid-run multi-GB image download")


def _boot_logs(d):
    return sorted(p.name for p in d.glob("*-tart-run.log"))


def test_boot_log_is_discarded_once_the_tests_return_a_verdict(
        tmp_path, monkeypatch):
    """A run that reached a verdict explains itself through the test output,
    so `tart run`'s log is noise and gets cleaned up."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(vm.tempfile, "gettempdir", lambda: str(logs))
    _wire(monkeypatch, _happy)

    exit_code, _ = _dispatch(tmp_path)

    assert exit_code == 0
    assert _boot_logs(logs) == []


def test_boot_log_survives_a_dispatch_that_never_reached_a_verdict(
        tmp_path, monkeypatch, caplog):
    """`tart run`'s output is the only account of what the guest was doing.
    Deleting it on an infrastructure failure leaves the next investigation
    with a character count and nothing else (spec_c8606c0c)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(vm.tempfile, "gettempdir", lambda: str(logs))

    def behave(cmd):
        if "xcodebuild test" in cmd[-1]:
            return subprocess.CompletedProcess(
                cmd, 255, stdout="", stderr="Connection closed")
        return _happy(cmd)

    _wire(monkeypatch, behave)

    with caplog.at_level("WARNING"):
        exit_code, output = _dispatch(tmp_path)

    assert exit_code is None
    assert "ssh transport failed" in output
    kept = _boot_logs(logs)
    assert len(kept) == 1, kept
    # The path is useless if the operator cannot find it.
    assert any(kept[0] in r.getMessage() for r in caplog.records), caplog.text


def _rsync_calls(calls):
    return [c for c in calls if any("rsync" in part for part in c)]


def test_no_package_cache_configured_means_no_push_and_no_flag(
        tmp_path, monkeypatch):
    """The default is the old behavior exactly: the guest resolves from the
    network and xcodebuild is left on its own DerivedData path."""
    monkeypatch.setattr(Config, "VM_PACKAGE_CACHE", "")
    calls = _wire(monkeypatch, _happy)

    exit_code, _ = _dispatch(tmp_path)

    assert exit_code == 0
    # Only the worktree rsync, no cache push.
    assert len(_rsync_calls(calls)) == 1, _rsync_calls(calls)


def test_configured_package_cache_is_pushed_before_resolution(
        tmp_path, monkeypatch):
    """Resolution must find the warm cache already on disk, so the push has to
    land before the resolve step runs (DEV-721)."""
    cache = tmp_path / "pkgcache"
    cache.mkdir()
    (cache / "repositories").write_text("warm")
    monkeypatch.setattr(Config, "VM_PACKAGE_CACHE", str(cache))
    calls = _wire(monkeypatch, _happy)

    exit_code, _ = _dispatch(tmp_path)

    assert exit_code == 0
    joined = [" ".join(c) for c in calls]
    push_i = next(i for i, j in enumerate(joined)
                  if "rsync" in j and vm.GUEST_PKG_CACHE in j)
    resolve_i = next(i for i, j in enumerate(joined)
                     if "resolvePackageDependencies" in j)
    assert push_i < resolve_i, joined


def test_an_empty_cache_directory_is_not_pushed(tmp_path, monkeypatch):
    """An empty tree would cost a round trip and warm nothing."""
    cache = tmp_path / "pkgcache"
    cache.mkdir()
    monkeypatch.setattr(Config, "VM_PACKAGE_CACHE", str(cache))
    calls = _wire(monkeypatch, _happy)

    exit_code, _ = _dispatch(tmp_path)

    assert exit_code == 0
    assert len(_rsync_calls(calls)) == 1, _rsync_calls(calls)


def test_a_failed_cache_push_costs_speed_not_the_run(tmp_path, monkeypatch):
    """Same contract as resolution itself: without the cache the guest fetches
    from the network, which is what it did before the cache existed."""
    cache = tmp_path / "pkgcache"
    cache.mkdir()
    (cache / "repositories").write_text("warm")
    monkeypatch.setattr(Config, "VM_PACKAGE_CACHE", str(cache))

    def behave(cmd):
        if any("rsync" in p for p in cmd) and vm.GUEST_PKG_CACHE in " ".join(cmd):
            return subprocess.CompletedProcess(
                cmd, 11, stdout="", stderr="guest disk full")
        return _happy(cmd)

    calls = _wire(monkeypatch, behave)

    exit_code, output = _dispatch(tmp_path)

    assert exit_code == 0
    assert "guest tests ok" in output
    # The test still ran despite the push failing.
    assert any("xcodebuild test" in " ".join(c) for c in calls)
