"""Ephemeral tart VM execution for frameworks sandbox-exec cannot contain.

DEV-422 (design: DEV-417, evidence: DEV-403): app-hosted XCTest hangs for the
full test-launch timeout under sandbox-exec regardless of profile, so those
runs dispatch into a throwaway macOS VM instead of running unsandboxed on the
host. The guest boots its own logged-in Aqua session, which is also what lets
XCUITest run at all (DEV-295 — on the host it dies with SIGKILL before
establishing connection).

Containment properties (spike DEV-421, Mac Studio 2026-08-03): separate
kernel, separate TCC domain, no host keychain/files reachable, and the guest
signs ad-hoc (CODE_SIGN_IDENTITY=-) — no signing identity, keychain, or
password ever enters the VM. Measured overhead: clone ~0 s (APFS
copy-on-write), boot-to-ssh ~19 s, 1.2 GB worktree rsync ~10 s; a warm test
run in the guest beat the host baseline.

The base image must be pulled once by the operator (`tart pull <image>`,
tens of GB) — this module never pulls, so a run can't stall on an 80 GB
download. Guest ssh uses the image's stock credentials via sshpass; the
guest is NAT-local and destroyed after every run, so the credentials guard
nothing durable.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import tempfile
import time
import uuid
from pathlib import Path

from .config import Config

logger = logging.getLogger("mac_runner.vm")

# Fixed guest-side layout. The cirruslabs images auto-log-in "admin".
GUEST_HOME = "/Users/admin"
GUEST_WORKTREE = f"{GUEST_HOME}/work"
GUEST_DERIVED_DATA = f"{GUEST_HOME}/dd"

_SSH_OPTS = [
    # Every guest is minted fresh, so its host key is always unknown; pinning
    # would just wedge the run. LogLevel=ERROR drops the per-connection
    # known-hosts warning that would otherwise land in every test output.
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=5",
    # Password auth ONLY, and none of the host's ssh identity: with an agent
    # loaded, ssh offers every key first and the guest cuts the connection at
    # MaxAuthTries — "Too many authentication failures" — before the password
    # is ever tried. Killed a real deploy run the moment the runner had an
    # agent in reach. -F /dev/null also keeps ~/.ssh/config's per-host rules
    # out of a connection to a throwaway NAT-local guest.
    "-F", "/dev/null",
    "-o", "IdentitiesOnly=yes",
    "-o", "IdentityAgent=none",
    "-o", "PubkeyAuthentication=no",
    "-o", "PreferredAuthentications=password",
    "-o", "NumberOfPasswordPrompts=1",
]

# Bounds for the non-test stages. The test step itself runs under the
# request's own timeout budget.
CLONE_TIMEOUT = 300
SYNC_TIMEOUT = 600
TEARDOWN_TIMEOUT = 60
# How long a dispatch waits for the single VM slot before refusing.
VM_SLOT_TIMEOUT = 30


class VMError(RuntimeError):
    pass


def vm_available() -> "str | None":
    """None when VM dispatch can proceed, else a human-actionable reason."""
    if shutil.which("tart") is None:
        return ("tart is not installed — brew trust cirruslabs/cli && "
                "brew install cirruslabs/cli/tart")
    if shutil.which("sshpass") is None:
        return "sshpass is not installed — brew install sshpass"
    listed = subprocess.run(["tart", "list"], capture_output=True, text=True)
    if listed.returncode != 0:
        return f"tart list failed: {listed.stderr.strip()}"
    if Config.VM_IMAGE not in listed.stdout:
        return (f"VM base image '{Config.VM_IMAGE}' is not pulled — run "
                f"`tart pull {Config.VM_IMAGE}` once (tens of GB) before "
                "dispatching these runs")
    return None


def _ssh_base(ip: str) -> list[str]:
    return ["sshpass", "-p", Config.VM_SSH_PASSWORD, "ssh", *_SSH_OPTS,
            f"{Config.VM_SSH_USER}@{ip}"]


def _guest_sh(cmd: list[str], cwd: str) -> str:
    """One shell word per argument, run from *cwd* in the guest.

    shlex quoting keeps xcodebuild settings like
    ``OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox`` literal — the guest
    shell must hand them to xcodebuild, not expand ``$(inherited)`` itself.
    """
    return f"cd {shlex.quote(cwd)} && {shlex.join(cmd)}"


def _boot_log(path: Path) -> str:
    """Tail of `tart run`'s own output, for failure messages."""
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "\n[tart run output]\n" + "\n".join(text.splitlines()[-20:])


def _wait_for_ssh(name: str, boot_proc: "subprocess.Popen",
                  boot_log: Path) -> str:
    deadline = time.monotonic() + Config.VM_BOOT_TIMEOUT
    ip = ""
    while time.monotonic() < deadline:
        if boot_proc.poll() is not None:
            raise VMError(
                f"tart run exited {boot_proc.returncode} before the guest "
                f"came up{_boot_log(boot_log)}")
        if not ip:
            probe = subprocess.run(["tart", "ip", name],
                                   capture_output=True, text=True)
            ip = probe.stdout.strip() if probe.returncode == 0 else ""
        if ip:
            # Early in boot the guest's TCP stack answers before sshd does,
            # so the probe can HANG rather than fail — seen on the first
            # real dispatch: connect succeeded, no banner, TimeoutExpired
            # escaped as a 500. A hung probe just means "not ready yet".
            try:
                ok = subprocess.run([*_ssh_base(ip), "true"],
                                    capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                continue
            if ok.returncode == 0:
                return ip
        time.sleep(2)
    raise VMError(
        f"guest not reachable over ssh within {Config.VM_BOOT_TIMEOUT}s "
        f"(CODING_MODEL_RUNNER_VM_BOOT_TIMEOUT); last ip={ip or '<none>'}"
        f"{_boot_log(boot_log)}")


# The runner is the only writer of this prefix, so a VM still carrying it
# once no run is in flight is by definition a leak (DEV-705).
VM_NAME_PREFIX = "cmr-"

# VM dispatch is serialised. tart has a system VM limit and discovering it by
# hitting it produces an opaque refusal at a DIFFERENT spec's dispatch, three
# seconds in, after that attempt already spent its context assembly and its
# write. One at a time is also simply what this host can do.
_VM_SLOT = threading.Lock()
_ACTIVE_VMS: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def list_runner_vms() -> "list[str]":
    """Every VM carrying the runner's prefix. Empty when tart cannot be read."""
    try:
        listed = subprocess.run(["tart", "list"], capture_output=True,
                                text=True, timeout=TEARDOWN_TIMEOUT)
    except Exception:
        logger.warning("tart list failed while looking for leaked VMs",
                       exc_info=True)
        return []
    if listed.returncode != 0:
        return []
    names = []
    for line in listed.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[1].startswith(VM_NAME_PREFIX):
            names.append(cols[1])
    return names


def sweep_leaked_vms(warnings: "list[str] | None" = None) -> "list[str]":
    """Reclaim runner VMs no live dispatch owns. Returns what was reclaimed.

    DEV-705: teardown is best-effort and a failed `tart delete` used to leak
    silently, so leaks accumulated until tart refused the next clone. Sweeping
    before claiming a slot means one bad teardown costs the NEXT run nothing
    instead of costing every run after it everything.

    Only VMs absent from _ACTIVE_VMS are touched, so a concurrent dispatch is
    never swept out from under itself.
    """
    with _ACTIVE_LOCK:
        active = set(_ACTIVE_VMS)
    leaked = [n for n in list_runner_vms() if n not in active]
    reclaimed = []
    for name in leaked:
        logger.warning("reclaiming leaked VM %s from an earlier run (DEV-705)",
                       name)
        _tart_quiet(["tart", "stop", name])
        if _tart_quiet(["tart", "delete", name]):
            reclaimed.append(name)
    if reclaimed and warnings is not None:
        warnings.append(
            f"[vm] reclaimed {len(reclaimed)} leaked VM(s) from an earlier "
            f"run before starting this one: {', '.join(reclaimed)}. A previous "
            "teardown did not complete (DEV-705).")
    return reclaimed


def _tart_quiet(argv: list[str]) -> bool:
    """Run a tart command, swallowing failure. True when it succeeded."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=TEARDOWN_TIMEOUT)
        return done.returncode == 0
    except Exception:
        logger.warning("%s failed", " ".join(argv), exc_info=True)
        return False


def _destroy(name: str, boot_proc: "subprocess.Popen | None",
             warnings: "list[str] | None" = None) -> None:
    """Best-effort, unconditional teardown — a leaked VM holds tens of GB.

    DEV-705: a teardown that fails now SAYS so on the response. The evidence
    used to live only in the Mac's log, on a host nobody is watching, and the
    first anyone knew of it was an unrelated spec dying on tart's VM limit.
    """
    stopped = _tart_quiet(["tart", "stop", name])
    if boot_proc is not None and boot_proc.poll() is None:
        boot_proc.kill()
    deleted = _tart_quiet(["tart", "delete", name])
    if deleted:
        return
    logger.error("tart delete %s failed — the VM is LEAKED; reclaim it with "
                 "`bash scripts/reclaim_tart_vms.sh --delete` (DEV-705)", name)
    if warnings is not None:
        warnings.append(
            f"[vm] teardown of {name} did not complete (stop "
            f"{'ok' if stopped else 'failed'}, delete failed) — the VM is "
            "leaked and will be reclaimed before the next dispatch. This is "
            "an infrastructure fault, not a fault in the code under test "
            "(DEV-705).")


def run_tests_in_vm(worktree: Path, resolve_cmd: "list[str] | None",
                    cmd: list[str], *, timeout: int,
                    resolve_timeout: int,
                    warnings: "list[str] | None" = None
                    ) -> "tuple[int | None, str]":
    """Run one test dispatch inside a throwaway VM; ALWAYS destroys it.

    Returns (exit_code, combined_output). exit_code None means the VM
    infrastructure failed or the budget ran out — not a test verdict.

    DEV-705: dispatch is serialised on _VM_SLOT and leaked VMs from earlier
    runs are swept before the clone, so this never discovers tart's system
    limit the hard way. Anything appended to *warnings* reaches the response
    as an infrastructure note rather than being read as the attempt's fault.
    """
    if not _VM_SLOT.acquire(timeout=VM_SLOT_TIMEOUT):
        # Refusing outright beats queueing behind a run whose budget we cannot
        # see, and beats letting tart refuse us with a message that names two
        # VM ids and no remedy.
        return None, (
            f"[vm] another VM dispatch has held the single VM slot for more "
            f"than {VM_SLOT_TIMEOUT}s — refusing to start a second one. This "
            "is an infrastructure limit on the runner host, not a fault in "
            "the code under test (DEV-705).")
    name = f"{VM_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
    boot_proc: "subprocess.Popen | None" = None
    boot_log: "Path | None" = None
    deadline = time.monotonic() + timeout
    resolve_output = ""
    try:
        sweep_leaked_vms(warnings)
        with _ACTIVE_LOCK:
            _ACTIVE_VMS.add(name)
        clone = subprocess.run(["tart", "clone", Config.VM_IMAGE, name],
                               capture_output=True, text=True,
                               timeout=CLONE_TIMEOUT)
        if clone.returncode != 0:
            return None, (f"[vm] tart clone '{Config.VM_IMAGE}' failed: "
                          f"{clone.stderr.strip()}")
        # tart run's own output is the ONLY explanation when a guest fails to
        # come up; discarding it leaves a 300 s timeout with no cause (seen
        # on the first launchd deploy). Kept on disk so the failure message
        # can quote it.
        boot_log = Path(tempfile.gettempdir()) / f"{name}-tart-run.log"
        with open(boot_log, "w") as boot_out:
            boot_proc = subprocess.Popen(
                ["tart", "run", name, "--no-graphics"],
                stdout=boot_out, stderr=subprocess.STDOUT)
        try:
            ip = _wait_for_ssh(name, boot_proc, boot_log)
        except VMError as e:
            return None, f"[vm] {e}"
        logger.info("vm %s up at %s", name, ip)

        sync = subprocess.run(
            ["sshpass", "-p", Config.VM_SSH_PASSWORD, "rsync", "-a", "--delete",
             "-e", "ssh " + " ".join(_SSH_OPTS),
             f"{worktree}/", f"{Config.VM_SSH_USER}@{ip}:{GUEST_WORKTREE}/"],
            capture_output=True, text=True, timeout=SYNC_TIMEOUT)
        if sync.returncode != 0:
            return None, f"[vm] worktree sync failed: {sync.stderr.strip()}"

        if resolve_cmd is not None:
            # Same contract as the host pre-step (DEV-294): a failed resolve
            # is non-fatal — the build may succeed from what the worktree
            # already carries — but it must be visible in the output.
            try:
                rr = subprocess.run(
                    [*_ssh_base(ip), _guest_sh(resolve_cmd, GUEST_WORKTREE)],
                    capture_output=True, text=True, timeout=resolve_timeout)
                if rr.returncode != 0:
                    logger.warning("in-vm package resolution exited %d",
                                   rr.returncode)
                    resolve_output = (
                        "[package resolution failed — the build may fail "
                        f"for this reason]\n{rr.stdout}\n{rr.stderr}\n\n")
            except subprocess.TimeoutExpired:
                logger.warning("in-vm package resolution timed out")
                resolve_output = "[package resolution timed out]\n\n"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, resolve_output + (
                f"Tests timed out after {timeout}s (boot/sync/resolve "
                "consumed the whole budget)")
        try:
            tr = subprocess.run(
                [*_ssh_base(ip), _guest_sh(cmd, GUEST_WORKTREE)],
                capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired as e:
            def _text(x: "bytes | str | None") -> str:
                return x.decode() if isinstance(x, bytes) else (x or "")
            return None, (resolve_output +
                          f"Tests timed out after {timeout}s\n"
                          f"{_text(e.stdout)}\n{_text(e.stderr)}")
        # ssh propagates the remote command's exit status; 255 is ssh's OWN
        # transport failure, which would otherwise masquerade as a test fail.
        if tr.returncode == 255:
            return None, (resolve_output + "[vm] ssh transport failed "
                          f"mid-run\n{tr.stdout}\n{tr.stderr}")
        return tr.returncode, (resolve_output + (tr.stdout or "") + "\n" +
                               (tr.stderr or ""))
    finally:
        try:
            _destroy(name, boot_proc, warnings)
            if boot_log is not None:
                boot_log.unlink(missing_ok=True)
        finally:
            # Drop ownership before releasing the slot, so the next dispatch's
            # sweep sees this VM as reclaimable if teardown left it behind.
            with _ACTIVE_LOCK:
                _ACTIVE_VMS.discard(name)
            _VM_SLOT.release()
