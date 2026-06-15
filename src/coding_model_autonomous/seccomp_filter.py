"""Seccomp-BPF denylist for the autonomous test sandbox.

The filter is loaded by executor._wrap_in_sandbox via bwrap's
``--seccomp <fd>``. Default action is ALLOW (denylist semantics): the
blocked syscalls cover namespace/mount escape, kernel module load,
ptrace, eBPF, io_uring, time manipulation, keyrings, and other
surfaces with frequent kernel CVEs.

A denylist is the right call here because tests are LLM-generated and
exercise arbitrary stdlib (multiprocessing, asyncio, urllib, ssl, ...).
An allowlist would block legitimate test patterns and require constant
maintenance; the denylist closes the actual CVE surface the audit cited
without that fragility.

The libseccomp Python binding ships as the apt package
``python3-seccomp`` on Ubuntu/Debian. The .so lands in
``/usr/lib/python3/dist-packages``, which is outside this venv (the
venv was created without ``--system-site-packages``). We try a normal
import first, then fall back to injecting that dist-packages dir into
``sys.path``. If neither works ``build_seccomp_bpf_fd()`` returns None
and the caller logs a warning + runs bwrap without ``--seccomp``.
"""
from __future__ import annotations

import errno
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_DIST_PACKAGES = "/usr/lib/python3/dist-packages"


def _try_import_seccomp():
    try:
        import seccomp
        return seccomp
    except ImportError:
        pass
    if os.path.isdir(_DIST_PACKAGES) and _DIST_PACKAGES not in sys.path:
        sys.path.insert(0, _DIST_PACKAGES)
        try:
            import seccomp
            return seccomp
        except ImportError:
            sys.path.remove(_DIST_PACKAGES)
    return None


_seccomp = _try_import_seccomp()
SECCOMP_AVAILABLE: bool = _seccomp is not None


DENYLIST: tuple[str, ...] = (
    # Namespace / mount manipulation. bwrap already created fresh
    # namespaces; deny any attempt to re-enter or remount.
    "unshare",
    "setns",
    "pivot_root",
    "mount",
    "umount",
    "umount2",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsmount",
    "fsconfig",
    "fspick",

    # Kernel module load/unload
    "init_module",
    "finit_module",
    "delete_module",
    "create_module",

    # Process introspection / debug — used to read or steal state
    # from the host's other processes if the user-namespace barrier
    # were ever weakened.
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "kcmp",

    # Kernel reload / shutdown
    "kexec_load",
    "kexec_file_load",
    "reboot",

    # Broad kernel attack surface — frequent CVEs.
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "io_uring_setup",
    "io_uring_register",
    "io_uring_enter",

    # Keyring
    "add_key",
    "request_key",
    "keyctl",

    # Time manipulation (could backdate to make a TLS cert valid).
    "settimeofday",
    "stime",
    "clock_settime",
    "clock_adjtime",
    "adjtimex",

    # Privileged admin
    "ioperm",
    "iopl",
    "quotactl",
    "swapon",
    "swapoff",

    # Container-escape primitives via filehandles.
    "name_to_handle_at",
    "open_by_handle_at",

    # Notification / audit
    "fanotify_init",
    "syslog",

    # Misc (obsolete-but-present, exploit primitives)
    "lookup_dcookie",
    "vmsplice",
    "uselib",
    "ustat",
    "nfsservctl",
    "get_kernel_syms",
    "query_module",
    "bdflush",
)


def build_seccomp_bpf_fd() -> Optional[int]:
    """Compile the denylist into BPF and return a memfd holding it.

    Caller passes the fd to bwrap via ``--seccomp <N>`` plus
    ``subprocess.run(pass_fds=(N,))`` and is responsible for closing
    the fd after the subprocess returns. Returns None when libseccomp
    isn't importable.
    """
    if _seccomp is None:
        return None

    f = _seccomp.SyscallFilter(_seccomp.ALLOW)

    blocked = 0
    skipped: list[str] = []
    for name in DENYLIST:
        try:
            f.add_rule(_seccomp.ERRNO(errno.EPERM), name)
            blocked += 1
        except (ValueError, RuntimeError):
            skipped.append(name)
    if skipped:
        logger.debug("seccomp: %d syscalls not present on this kernel: %s",
                     len(skipped), ",".join(skipped))

    bpf_fd = os.memfd_create("qwen-seccomp", flags=0)
    # export_bpf wants a file-like object (it calls .fileno()); wrap the
    # memfd with closefd=False so the bare fd survives the `with` block.
    with os.fdopen(bpf_fd, "wb", closefd=False) as f_obj:
        f.export_bpf(f_obj)
    os.lseek(bpf_fd, 0, os.SEEK_SET)
    logger.info("seccomp: built filter (default ALLOW, %d syscalls denied)", blocked)
    return bpf_fd
