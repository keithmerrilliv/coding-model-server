"""The seccomp denylist really denies, and only what it lists.

seccomp_filter builds the BPF program that test_runner hands bwrap as
``--seccomp <fd>`` for every LLM-generated test run. Until now its only test
reference stubbed it out, so nothing showed that the program the kernel loads
refuses a listed syscall. These tests run a probe child through
``test_runner._run_confined``, the path every local test run takes, once with
the filter and once without it. Without the filter, the probe syscalls succeed
inside the same bwrap sandbox. That control is what shows an EPERM under the
filter comes from the filter and not from bwrap's namespaces or the host.

The probe calls are chosen because they are harmless when they succeed: kcmp
compares the probe with itself, userfaultfd asks for user-mode-only faults,
io_uring_setup makes a one-entry ring, and keyctl asks for the session keyring
id. Each returns an fd or an id, which the child's exit closes.
"""
import json
import os
import shutil
import sys

import pytest

from coding_model_autonomous import seccomp_filter, test_runner

# name -> integer args after the syscall number. "PID" and "BUF" are filled in
# by the probe: its own pid, and a zeroed 120-byte io_uring_params struct.
_PROBES = {
    "kcmp": ["PID", "PID", 1, 0, 0],           # KCMP_VM, self against self
    "userfaultfd": [0o2000000 | 1],            # O_CLOEXEC | UFFD_USER_MODE_ONLY
    "io_uring_setup": [1, "BUF"],
    "keyctl": [0, -3, 0, 0, 0],                # GET_KEYRING_ID, session keyring
}

_PROBE_SRC = r'''
import ctypes, errno, json, os, sys

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
spec = json.loads(sys.argv[1])
buf = ctypes.create_string_buffer(120)

def arg(a):
    if a == "PID":
        return os.getpid()
    if a == "BUF":
        return ctypes.addressof(buf)
    return a

results = {}
for name, (nr, args) in spec.items():
    ctypes.set_errno(0)
    rc = libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(arg(a)) for a in args])
    err = ctypes.get_errno()
    results[name] = "ok" if rc >= 0 else errno.errorcode.get(err, str(err))

# Ordinary work after the denials: the process is still alive and unharmed.
with open("written_under_filter.txt", "w") as f:
    f.write("still here")
results["_allowed"] = open("written_under_filter.txt").read() == "still here" \
    and os.getpid() > 0 and bool(os.uname().sysname)
print("PROBE " + json.dumps(results))
'''


def _require_sandbox():
    if not sys.platform.startswith("linux"):
        pytest.skip("seccomp and bwrap are Linux-only")
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap (bubblewrap) is not installed")
    if not seccomp_filter.SECCOMP_AVAILABLE:
        pytest.skip("libseccomp's Python binding (python3-seccomp) is not importable")


def _syscall_table():
    sc = seccomp_filter._seccomp
    table = {}
    for name, args in _PROBES.items():
        try:
            table[name] = [sc.resolve_syscall(sc.Arch.NATIVE, name), args]
        except (ValueError, RuntimeError):
            continue          # not a syscall on this architecture
    if not table:
        pytest.skip("none of the probe syscalls exists on this architecture")
    return table


def _probe(spec_dir, monkeypatch, *, with_filter: bool) -> dict:
    """Run the probe through test_runner._run_confined; return its results."""
    monkeypatch.delenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", raising=False)
    if not with_filter:
        monkeypatch.setattr(seccomp_filter, "build_seccomp_bpf_fd", lambda: None)
    ok, output = test_runner._run_confined(
        [sys.executable, "-c", _PROBE_SRC, json.dumps(_syscall_table())],
        spec_dir, 60, what="seccomp probe")
    line = next((ln for ln in output.splitlines() if ln.startswith("PROBE ")), None)
    if line is None:
        if "bwrap" in output or "Operation not permitted" in output:
            pytest.skip(f"bwrap cannot create a sandbox here: {output[-300:]}")
        pytest.fail(f"the probe printed no result (ok={ok}):\n{output}")
    assert ok, output
    return json.loads(line[len("PROBE "):])


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec"
    d.mkdir()
    return d


# ── the kernel enforces it ───────────────────────────────────────────────────

@pytest.mark.slow
def test_listed_syscalls_fail_with_eperm_under_the_filter(spec_dir, monkeypatch):
    _require_sandbox()
    results = _probe(spec_dir, monkeypatch, with_filter=True)
    probed = {k: v for k, v in results.items() if not k.startswith("_")}
    assert probed, "no probe syscall ran"
    assert probed == {name: "EPERM" for name in probed}


@pytest.mark.slow
def test_the_process_survives_and_ordinary_work_still_runs(spec_dir, monkeypatch):
    """ERRNO, not KILL: a test that trips the denylist gets an error it can
    report, and the syscalls a test legitimately makes still work."""
    _require_sandbox()
    results = _probe(spec_dir, monkeypatch, with_filter=True)
    assert results["_allowed"] is True
    assert (spec_dir / "written_under_filter.txt").read_text() == "still here"


@pytest.mark.slow
def test_the_same_syscalls_succeed_in_the_sandbox_without_the_filter(
        spec_dir, monkeypatch):
    """The control: bwrap alone lets these through, so the EPERM above is the
    filter's doing. A host that refuses one anyway (io_uring disabled by
    sysctl, say) drops it from the comparison rather than failing."""
    _require_sandbox()
    unfiltered = _probe(spec_dir, monkeypatch, with_filter=False)
    monkeypatch.undo()
    filtered = _probe(spec_dir, monkeypatch, with_filter=True)

    let_through = [k for k, v in unfiltered.items()
                   if not k.startswith("_") and v != "EPERM"]
    if not let_through:
        pytest.skip(f"this host refuses every probe syscall without the "
                    f"filter, so the control is inconclusive: {unfiltered}")
    for name in let_through:
        assert filtered[name] == "EPERM", (name, unfiltered[name], filtered[name])


# ── the filter object ────────────────────────────────────────────────────────

def test_the_denylist_names_the_escape_and_cve_surfaces():
    for name in ("unshare", "setns", "mount", "ptrace", "bpf", "io_uring_setup",
                 "userfaultfd", "kexec_load", "init_module", "keyctl",
                 "clock_settime", "open_by_handle_at"):
        assert name in seccomp_filter.DENYLIST
    assert len(set(seccomp_filter.DENYLIST)) == len(seccomp_filter.DENYLIST)


def test_the_dist_packages_fallback_never_shadows_the_venv(monkeypatch):
    """The fallback leaves dist-packages on sys.path for the life of the
    process, so it has to sit behind the venv. At the front it served apt's
    regex 2025.9.18 to transformers, whose version check then failed (DEV-917)."""
    path = [p for p in sys.path if p != seccomp_filter._DIST_PACKAGES]
    path.insert(0, "/venv-stand-in")
    monkeypatch.setattr(sys, "path", path)
    monkeypatch.delitem(sys.modules, "seccomp", raising=False)
    seccomp_filter._try_import_seccomp()
    if seccomp_filter._DIST_PACKAGES in sys.path:
        assert sys.path[-1] == seccomp_filter._DIST_PACKAGES
    assert sys.path[0] == "/venv-stand-in"


def test_no_binding_means_no_fd(monkeypatch):
    """test_runner reads None as 'run bwrap without --seccomp' and says so."""
    monkeypatch.setattr(seccomp_filter, "_seccomp", None)
    assert seccomp_filter.build_seccomp_bpf_fd() is None


def test_the_fd_holds_a_bpf_program_from_its_start():
    """bwrap reads the program from the fd's current offset, so it must be
    rewound. A classic BPF program is a whole number of 8-byte instructions."""
    _require_sandbox()
    fd = seccomp_filter.build_seccomp_bpf_fd()
    assert fd is not None
    try:
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        program = os.read(fd, 1 << 20)
    finally:
        os.close(fd)
    assert program and len(program) % 8 == 0
