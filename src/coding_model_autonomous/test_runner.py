"""Test execution for autonomous specs: sandboxing and dispatch.

Extracted verbatim from executor.py (DEV-152). This is the security-relevant
layer — bubblewrap + seccomp confinement of LLM-generated tests, and the
local-vs-mac-runner dispatch — and it was previously unreachable for unit
testing without importing the whole 2,300-line executor (and its import-time
load_dotenv/basicConfig). It no longer depends on executor at all.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import requests
from dataclasses import dataclass, field
from pathlib import Path
from typing import Collection, Iterable, Optional

from . import seccomp_filter
from .workspace import CONTAINED_DIR

# Mac-runner dispatch session only. Inference calls never touch this — they
# go through _http.post_chat_completion, which pools its own connections.
_SESSION = requests.Session()

logger = logging.getLogger("orchestrator.test_runner")


# ── Test runner ──────────────────────────────────────────────────────────────

def _sandbox_available() -> bool:
    """True if we can sandbox test execution with bubblewrap on this host."""
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def seccomp_preflight() -> "tuple[bool, str]":
    """(fully_sandboxed, detail) — for a LOUD one-time daemon-startup check.

    A missing libseccomp used to surface as one log line per test run,
    buried mid-spec: bwrap kept running but the kernel-CVE surface the
    filter targets (io_uring, userfaultfd, ptrace) was silently re-exposed
    (DEV-155). The daemon logs this prominently at startup and can be made
    to refuse via CODING_MODEL_REQUIRE_SECCOMP=1.
    """
    if not _sandbox_available():
        return False, ("bwrap (bubblewrap) not found — local test runs will "
                       "refuse unless CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1")
    fd = seccomp_filter.build_seccomp_bpf_fd()
    if fd is None:
        return False, ("libseccomp unavailable (install python3-seccomp) — "
                       "bwrap will run WITHOUT the kernel-syscall denylist")
    try:
        os.close(fd)
    except OSError:
        pass
    return True, "bwrap+seccomp"


# Mountpoint (inside the sandbox) where the Node toolchain is bound for the
# `node_test` framework. Must be TOP-LEVEL: the baseline binds mount /opt
# read-only, so bwrap cannot mkdir a bind target nested under it.
_SANDBOX_NODE_MOUNT = "/coding-model-node"


def _resolve_sandbox_node_root() -> Optional[Path]:
    """Locate a Node install root to bind into the sandbox for `node --test`.

    Prefers the explicit CODING_MODEL_SANDBOX_NODE_ROOT (required in practice:
    the orchestrator's systemd PATH usually has no Node, and nvm installs it
    under /home, which the sandbox masks with tmpfs). Falls back to the Node the
    orchestrator process itself can see on PATH. Returns None if no usable Node
    root is found — `node_test` then fails with a clear diagnostic.
    """
    explicit = os.getenv("CODING_MODEL_SANDBOX_NODE_ROOT", "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        return root if (root / "bin" / "node").exists() else None
    node = shutil.which("node")
    if node:
        # <root>/bin/node  ->  <root>
        return Path(node).resolve().parent.parent
    return None


SANDBOX_NODE_ROOT = _resolve_sandbox_node_root()


def _resolv_conf_bind() -> list[str]:
    """bwrap args making DNS work in a `--share-net` sandbox.

    `/etc` is bound read-only, which looks like it should be enough — but on
    systemd-resolved hosts (Ubuntu/Debian default) `/etc/resolv.conf` is a
    SYMLINK into `/run/systemd/resolve/`, and `/run` is not bound. The link
    therefore dangles inside the sandbox, every lookup fails with EAI_AGAIN,
    and npm hangs retrying until it hits the install timeout with no output at
    all — a silent, very confusing failure (DEV-104).

    The fix is to bind the symlink's TARGET at its own path, so the link that
    `/etc` already provides resolves. Binding over `/etc/resolv.conf` directly
    does NOT work: bwrap cannot create a mount point on a dangling symlink and
    aborts with "Can't create file at /etc/resolv.conf".

    The stub resolver it points at (127.0.0.53) is reachable because
    `--share-net` keeps the host's network namespace, loopback included.
    """
    src = Path("/etc/resolv.conf")
    try:
        real = src.resolve()
    except OSError:
        return []
    if real == src or not real.exists():
        # A plain file: already covered by the read-only /etc bind.
        return []
    return ["--ro-bind-try", str(real), str(real)]


def _wrap_in_sandbox(
    cmd: list[str],
    spec_dir: Path,
    seccomp_fd: Optional[int] = None,
    *,
    share_net: bool = False,
    extra_binds: Optional[list[str]] = None,
) -> list[str]:
    """Wrap `cmd` in a bubblewrap sandbox.

    The sandbox denies the LLM-generated tests access to anything outside the
    spec workspace:

      - `--unshare-all` creates fresh user/ipc/pid/uts/cgroup/net namespaces,
        so the tests cannot see host processes and have no network (not even
        loopback).
      - `/home` and `/root` are masked with tmpfs so secrets (`.env`, `.ssh`,
        API tokens, browser profiles, etc.) are invisible.
      - `/usr`, `/etc`, `/bin`, `/lib*`, `/opt` are bound read-only so Python
        and pytest can still import system libraries.
      - The venv holding the running Python + pytest is bound read-only.
      - `spec_dir` is bound read-write so pytest can create `.pytest_cache`
        and tests can write their own fixtures.
      - `--clearenv` strips inherited env vars — tests see a minimal,
            predictable environment.
      - When *seccomp_fd* is provided, bwrap loads a libseccomp BPF denylist
        from that fd just before exec(), blocking ~50 dangerous syscalls
        (mount/unshare/setns, ptrace, bpf, io_uring, kexec, keyctl, time
        manipulation, etc.). See ``seccomp_filter.DENYLIST``.

    Tests that legitimately need network or host access won't work under this
    sandbox; set CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1 to opt out at your own risk.

    *share_net* re-shares ONLY the network namespace (bwrap's `--share-net`,
    which is defined exactly as a modifier to `--unshare-all`). It exists for
    one caller — the dependency-install phase of `_provision_node_modules`
    (DEV-104), which has to reach the npm registry. Every other confinement
    stays in force for that phase: `/home` and `/root` are still tmpfs-masked,
    so `~/.npmrc` (and the auth tokens in it), `~/.ssh` and `.env` files remain
    invisible, and only *spec_dir* is writable. TEST execution never sets it —
    a test that could reach the network could exfiltrate whatever it read.

    *extra_binds* are raw bwrap arguments spliced in before the *spec_dir*
    bind, for the optional persistent npm cache (see _npm_cache_args).
    """
    # Walk up from sys.executable WITHOUT resolving symlinks — venv pythons
    # are typically a symlink chain (`venv/bin/python -> python3 -> /usr/bin/python3`)
    # and .resolve() follows it all the way to /usr, so `--ro-bind /usr /usr`
    # would replace the venv bind and `sys.executable`'s own path would be
    # invisible inside the sandbox.
    venv_root = Path(sys.executable).absolute().parent.parent
    spec_abs = spec_dir.resolve()

    # Optionally bind a Node toolchain into the sandbox so `node_test` specs can
    # run `node --test`. The bind SOURCE is resolved on the host here, before
    # the `/home` tmpfs mask is applied inside the sandbox, so an nvm path under
    # /home works as the source. The mountpoint is top-level (see
    # _SANDBOX_NODE_MOUNT) and prepended to PATH.
    node_bind: list[str] = []
    sandbox_path = "/usr/local/bin:/usr/bin:/bin"
    if SANDBOX_NODE_ROOT is not None:
        node_bin = SANDBOX_NODE_ROOT / "bin"
        if str(node_bin) in ("/usr/bin", "/usr/local/bin", "/bin"):
            # System Node already lives on a bound, on-PATH directory.
            pass
        else:
            node_bind = ["--ro-bind", str(SANDBOX_NODE_ROOT), _SANDBOX_NODE_MOUNT]
            sandbox_path = f"{_SANDBOX_NODE_MOUNT}/bin:{sandbox_path}"

    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
        "--setenv", "PATH", sandbox_path,
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONUNBUFFERED", "1",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        # Deterministic, parseable output. Both jest and vitest colourise and
        # animate when they think they're on a TTY, and the ANSI escapes land
        # in the middle of the very summary lines the structural guard matches
        # (orchestrator_daemon._validate_test_output_structure). They honour
        # these two vars regardless of TTY detection.
        "--setenv", "CI", "true",
        "--setenv", "NO_COLOR", "1",
        # Baseline filesystem — read-only
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/lib32", "/lib32",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/etc", "/etc",
        "--ro-bind-try", "/opt", "/opt",
        # Fresh kernel interfaces and writable tmp
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
        # Hide every user home, then re-expose just what we need
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--ro-bind", str(venv_root), str(venv_root),
        *node_bind,
        *(extra_binds or []),
        "--bind", str(spec_abs), str(spec_abs),
        "--chdir", str(spec_abs),
    ]
    # `--share-net` is only meaningful after `--unshare-all`, which is why it
    # is appended here rather than swapped in above.
    if share_net:
        args.append("--share-net")
        args.extend(_resolv_conf_bind())
    if seccomp_fd is not None:
        args.extend(["--seccomp", str(seccomp_fd)])
    args.append("--")
    args.extend(cmd)
    return args


# Per-framework default timeouts (seconds). Swift/Xcode builds are slow,
# especially cold, so their defaults are generous.
#
# DEV-705, 2026-09-18: THIS TABLE AND mac_runner/frameworks.py MUST AGREE for any
# framework in both. The effective budget is the MINIMUM of the two, because this
# side abandons the dispatch while the runner is still working, so a raise on one
# host alone does nothing. DEV-705 raised xcodebuild_test to 1200 in
# mac_runner/frameworks.py and not here, and the raise was inert for a day — run
# 42 dispatched at 900s while the Mac was prepared to allow 1200s. The record said
# the problem was fixed, which is worse than it being open.
#
# The value itself is the runner's reasoning: the VM path spends the budget on
# boot, worktree sync and package resolution before the test starts, and a resolve
# that times out alone costs its whole budget — one run reached 823.4s of overhead,
# 77s short of the old 900s ceiling, so a passing test was seconds away from being
# reported as a timeout.
#
# DEV-752 considered raising this and did not: the Mac's phase timings showed run
# 44 was a starved host, not a slow resolve, and a bigger ceiling only lets a
# starved run burn longer before reporting. The runner now refuses rather than
# starting a doomed test (server.resolve_budget, MIN_TEST_BUDGET).
#
# test_timeouts_agree_across_hosts pins the two tables together.
DEFAULT_TIMEOUTS: dict[str, int] = {
    "pytest": 120,
    "python": 120,
    "jest": 180,
    "vitest": 180,
    "node_test": 120,
    "swift_test": 300,
    "xcodebuild_test": 1200,
}

# Frameworks whose tests import from `node_modules`, so the spec needs a
# dependency-install phase before they can run (DEV-104). `node_test` is
# deliberately NOT here: DEV-103's zero-dependency path must never reach the
# network, and keeping it out is what guarantees that.
NODE_MODULES_FRAMEWORKS: frozenset[str] = frozenset({"jest", "vitest"})

# Budget for `npm ci`/`npm install`. Separate from the test timeout: a cold
# React install fetches a few hundred MB and would otherwise eat the budget
# the tests themselves need.
NPM_INSTALL_TIMEOUT = int(os.getenv("CODING_MODEL_NPM_INSTALL_TIMEOUT", "300"))

KNOWN_FRAMEWORKS: frozenset[str] = frozenset(DEFAULT_TIMEOUTS) | {"none"}

# Names an LLM planner reaches for when asked to test Apple code. None of these
# are dispatch keys, and an unmapped one silently became a local pytest run
# (DEV-392), so map them onto the real Mac-runner frameworks. `xctest` means the
# XCTest bundle of an Xcode scheme, which is xcodebuild_test.
_APPLE_FRAMEWORK_ALIASES: dict[str, str] = {
    "xctest": "xcodebuild_test",
    "xcode": "xcodebuild_test",
    "xcodebuild": "xcodebuild_test",
    "swift": "swift_test",
    "swiftpm": "swift_test",
    "swift-testing": "swift_test",
}

MAC_RUNNER_URL = os.getenv("MAC_RUNNER_URL", "http://127.0.0.1:5050")
MAC_RUNNER_API_KEY = os.getenv("MAC_RUNNER_API_KEY", "")

# Relative paths inside spec_dir that should never be shipped to the Mac
# runner as patch content (they're not part of the LLM's diff).
# retry_history holds a full snapshot of every prior attempt
# (_snapshot_retry): shipping it made mac-runner patch payloads grow
# linearly with retries and materialized N stale copies of every source
# file in the git worktree — duplicate-source compilation in glob-based
# SPM targets (DEV-196).
# Merged repo+workspace tree for self-target pytest specs (DEV-626). Lives
# inside spec_dir so the existing RW bind covers it; must never ride along in
# a delivery patch set or a retry snapshot — hence its place in the skips.
_REPO_OVERLAY_DIR = ".repo_overlay"

_SPEC_SKIP_PATTERNS = (".pytest_cache", "__pycache__", ".DS_Store",
                       "test_output.txt", "retry_history", _REPO_OVERLAY_DIR,
                       # DEV-738: a contained colliding artifact is kept for
                       # inspection and must participate in no run. Skipping
                       # it here is what makes that true for every framework
                       # at once, rather than per-language in the ledger.
                       CONTAINED_DIR)
# DEV-688: the workspace's own source tree. Never a pytest collection
# target — the overlay puts it on PYTHONPATH instead.
_WORKSPACE_SRC_DIR = "src"
# DEV-689: a repository test that spawns its own bwrap sandbox, git checkout
# or npm install cannot run INSIDE the pre-gate sandbox — no network, and the
# nested confinement fails. Such a file declares this identifier at module
# level and the existing-tests selection skips it, so it never reds an
# attempt for something the model did not do.
NOT_IN_SANDBOX_MARKER = "PREGATE_SANDBOX_UNSAFE"


# Cap on captured output. Without this, a runaway test that prints a
# tight loop of MB/s straight to stdout buffers everything in memory
# and OOMs the orchestrator. 4 MiB is plenty for real test traces.
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def _truncate(s: str) -> str:
    b = s.encode("utf-8", errors="replace")
    if len(b) <= MAX_OUTPUT_BYTES:
        return s
    return (
        b[: MAX_OUTPUT_BYTES // 2].decode("utf-8", errors="replace")
        + f"\n... [truncated {len(b) - MAX_OUTPUT_BYTES} bytes of output] ...\n"
        + b[-MAX_OUTPUT_BYTES // 2 :].decode("utf-8", errors="replace")
    )


def _run_confined(
    raw_cmd: list[str],
    spec_dir: Path,
    timeout: int,
    *,
    what: str,
    share_net: bool = False,
    extra_binds: Optional[list[str]] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    """Run *raw_cmd* under bwrap+seccomp; return (exited_zero, combined_output).

    Extracted from _run_local_tests (DEV-104) so the dependency-install phase
    gets the identical confinement and process handling — the killpg-on-timeout
    and output cap below matter at least as much for `npm install`, which
    happily spawns a tree of children and can emit unbounded progress output.

    *share_net* is forwarded to _wrap_in_sandbox; only the install phase sets
    it. *what* names the activity for logs and the bwrap-missing diagnostic.

    *extra_env* vars reach the child in BOTH confinement modes: as `--setenv`
    bwrap args under the sandbox (which `--clearenv`s everything else), and
    merged over os.environ on the unsandboxed opt-out path (DEV-626).
    """
    allow_unsandboxed = os.getenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "").lower() in ("1", "true", "yes")

    # The env var takes priority over bwrap detection: if the user explicitly
    # opted out, honor it — even when bwrap is installed but broken (e.g.
    # AppArmor restricting unprivileged user namespaces, which silently
    # makes every bwrap invocation fail with "Operation not permitted"
    # before pytest gets a chance to run).
    bpf_fd: Optional[int] = None
    if allow_unsandboxed:
        cmd = raw_cmd
        sandbox_mode = "UNSANDBOXED (CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1)"
        logger.warning(
            "running LLM-generated %s WITHOUT a sandbox — it has full "
            "access to this user's environment", what
        )
    elif _sandbox_available():
        bpf_fd = seccomp_filter.build_seccomp_bpf_fd()
        sandbox_extra = list(extra_binds or [])
        for key, value in (extra_env or {}).items():
            sandbox_extra += ["--setenv", key, value]
        cmd = _wrap_in_sandbox(raw_cmd, spec_dir, seccomp_fd=bpf_fd,
                               share_net=share_net,
                               extra_binds=sandbox_extra or None)
        if bpf_fd is None:
            sandbox_mode = "bwrap (no seccomp — libseccomp unavailable)"
            logger.warning(
                "seccomp filter unavailable; bwrap will run without --seccomp. "
                "Install python3-seccomp on the host to enable kernel-syscall "
                "filtering for LLM-generated tests."
            )
        else:
            sandbox_mode = "bwrap+seccomp"
        if share_net:
            sandbox_mode += " +net"
    else:
        msg = (
            f"Refusing to run LLM-generated {what}: bwrap (bubblewrap) is not "
            "available and CODING_MODEL_ALLOW_UNSANDBOXED_TESTS is not set. Install "
            "bubblewrap (e.g. `apt install bubblewrap` on Debian/Ubuntu) on "
            "the Linux server, or set CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1 to opt "
            "out (not recommended — tests run with the orchestrator's own "
            "privileges)."
        )
        logger.error(msg)
        return False, msg

    logger.info("running %s via %s: %s (timeout=%ds)",
                what, sandbox_mode, " ".join(raw_cmd), timeout)

    try:
        # Popen + communicate instead of subprocess.run: run()'s timeout
        # kills only the DIRECT child, then blocks draining stdout — an
        # LLM-written test that spawned a background process leaves an
        # orphan holding the pipe, and the drain hangs the tick thread
        # forever with no heartbeat (DEV-155). Own session + killpg takes
        # the whole group down, after which the drain returns immediately.
        proc = subprocess.Popen(
            cmd, cwd=spec_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
            pass_fds=(bpf_fd,) if bpf_fd is not None else (),
            env={**os.environ, **extra_env} if allow_unsandboxed and extra_env else None,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            output = _truncate(out or "") + "\n" + _truncate(err or "")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            # Group is dead; the drain returns. Surface partial output so
            # the supervisor / reviewer can diagnose the hang.
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                out = e.stdout if isinstance(e.stdout, str) else ""
                err = e.stderr if isinstance(e.stderr, str) else ""
            output = (
                f"{what} timed out after {timeout}s\n"
                f"--- partial stdout ---\n{_truncate(out or '')}\n"
                f"--- partial stderr ---\n{_truncate(err or '')}"
            )
            ok = False
    except Exception as e:
        output = f"Test runner failed running {what}: {type(e).__name__}: {e}"
        ok = False
    finally:
        if bpf_fd is not None:
            try:
                os.close(bpf_fd)
            except OSError:
                pass

    return ok, output.strip()


def _npm_cache_args(spec_dir: Path) -> tuple[list[str], list[str]]:
    """(bwrap binds, npm flags) for the install phase's package cache.

    By default the cache lives under HOME=/tmp, which is a tmpfs inside the
    sandbox — so every spec re-downloads its dependency tree from scratch and
    nothing survives to be tampered with between runs. Setting
    CODING_MODEL_NPM_CACHE_DIR binds a persistent host directory instead,
    which turns a cold multi-minute React install into a warm one. That is a
    deliberate trade: the cache is then shared mutable state across specs.
    npm still verifies every entry against its integrity hash on read, and
    `--ignore-scripts` means cached content cannot execute at install time,
    so a poisoned entry would have to survive as ordinary imported code —
    which is exactly the position we are already in with LLM-authored tests.
    """
    raw = os.getenv("CODING_MODEL_NPM_CACHE_DIR", "").strip()
    if not raw:
        return [], []
    cache = Path(raw).expanduser()
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("CODING_MODEL_NPM_CACHE_DIR=%s is unusable (%s) — "
                       "falling back to an ephemeral in-sandbox cache", cache, e)
        return [], []
    return ["--bind", str(cache), str(cache)], ["--cache", str(cache)]


def _provision_node_modules(spec_dir: Path) -> tuple[bool, str]:
    """Install a jest/vitest spec's dependencies in a network-gated sandbox.

    DEV-104. This is the ONLY phase of the pipeline that is allowed to reach
    the network, and it is confined on every other axis: `/home` and `/root`
    are tmpfs-masked (so the operator's `~/.npmrc` registry tokens, `~/.ssh`
    and `.env` files are not visible to it), only *spec_dir* is writable, and
    the same seccomp denylist applies.

    `--ignore-scripts` is passed UNCONDITIONALLY and has no override. npm
    lifecycle hooks (preinstall/install/postinstall/prepare) are arbitrary
    code execution by whatever the LLM happened to name in `package.json` —
    the precise thing this sandbox exists to prevent — and they run at install
    time, when we are still holding the network open. A CLI flag also
    outranks a project-local `.npmrc`, so a spec cannot re-enable them by
    writing `ignore-scripts=false` next to its `package.json`. The cost is
    packages with native build steps (node-gyp); React, jest, vitest, jsdom
    and Testing Library are all pure JS and unaffected.

    Returns (ok, output). A no-op success when the spec has no package.json.
    """
    if not (spec_dir / "package.json").is_file():
        # Nothing declared. Let the runner proceed — the framework's own
        # "cannot find module" error is a better diagnostic than anything
        # we would invent here.
        return True, ""

    cache_binds, cache_flags = _npm_cache_args(spec_dir)

    # `npm ci` is the pinned, reproducible path, but it hard-fails when the
    # lockfile disagrees with package.json — which is the normal outcome when
    # a model hand-writes both. Planner guidance tells specs not to author a
    # lockfile; when one is absent we resolve fresh instead.
    has_lock = (spec_dir / "package-lock.json").is_file()
    raw_cmd = [
        "npm", "ci" if has_lock else "install",
        "--ignore-scripts",
        "--no-audit", "--no-fund",
        "--loglevel", "warn",
        *cache_flags,
    ]

    ok, output = _run_confined(
        raw_cmd, spec_dir, NPM_INSTALL_TIMEOUT,
        what="dependency install", share_net=True, extra_binds=cache_binds,
    )
    if not ok:
        return False, (
            f"Dependency install failed (`{' '.join(raw_cmd)}`). Tests cannot "
            f"run without node_modules. Note that install scripts are disabled "
            f"and the sandbox has no access to anything outside the spec "
            f"directory.\n\n{output}"
        )
    logger.info("provisioned node_modules for %s", spec_dir.name)
    return True, output


_SERVER_REPO_ROOT = Path(__file__).resolve().parents[2]


def _extract_committed_src(repo_root: Path, into: Path) -> None:
    """`git archive HEAD src` extracted under *into* (yielding into/src).

    Raises on any failure — a missing git, a directory that is not a
    checkout, an unborn HEAD — so the caller can fall back loudly.
    """
    import io
    import tarfile
    out = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar",
                          "HEAD", "src"], capture_output=True, check=True, timeout=60)
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
        tar.extractall(path=into, filter="data")


def _src_tree_state(repo_root: Path) -> tuple[str, list[str]]:
    """(short HEAD sha, paths under src/ with uncommitted changes)."""
    sha = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    status = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain",
                             "--", "src"], capture_output=True, text=True, timeout=30)
    dirty = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
    return sha, dirty


def _materialize_local_repo_overlay(spec_dir: Path, repo: Optional[str]) -> Optional[Path]:
    """Make a self-target repo's package importable inside the sandbox (DEV-626).

    The sandbox binds only the venv and the spec workspace, so a pytest spec
    whose test_strategy.repo names this repo itself collects straight to
    `ModuleNotFoundError: No module named 'coding_model_autonomous'` — every
    attempt, however good, reds the same way (run 20 burned its whole retry
    rotation against that wall).

    Pure PYTHONPATH layering cannot fix it: the workspace holds only the
    EDITED files, and the first regular package dir found shadows the whole
    package. So build a merged tree — copy the repo's src/ packages, then
    overlay the workspace's own src/ files on top. The ordering is the point:
    the workspace copy must shadow the repo copy, or the pre-gate check tests
    shipped code instead of the candidate (the DEV-602 tested-vs-shipped
    concern in miniature). The tree lives inside spec_dir, which is already
    RW-bound into the sandbox; callers put the returned path on PYTHONPATH.

    Returns None when the spec doesn't target this repo, or when the repo
    layout is unrecognisable.
    """
    if not repo or repo != _SERVER_REPO_ROOT.name:
        return None
    repo_src = _SERVER_REPO_ROOT / "src"
    if not repo_src.is_dir():
        return None
    overlay_src = spec_dir / _REPO_OVERLAY_DIR / "src"
    if overlay_src.exists():
        shutil.rmtree(overlay_src)  # rebuilt fresh each run — never stale
    # DEV-654: the overlay is the COMMITTED tree, not the working tree. This
    # checkout is the one a human (or another Claude instance) edits while a
    # run is in flight; copying it meant an uncommitted edit to a file the
    # spec never touched became what the candidate was tested against — a
    # half-finished daemon edit reds an executor.py spec, and an uncommitted
    # fix can green a candidate that fails at HEAD. Nothing recorded which.
    # `git archive HEAD src` is exactly the state the spec was planned
    # against; the working tree is an explicit opt-in.
    if os.getenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", "") == "1":
        shutil.copytree(repo_src, overlay_src,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        logger.warning("repo overlay built from the WORKING TREE by explicit "
                       "opt-in (AUTONOMOUS_OVERLAY_FROM_WORKING_TREE=1) — the "
                       "sandbox sees uncommitted edits (DEV-654)")
    else:
        try:
            _extract_committed_src(_SERVER_REPO_ROOT, overlay_src.parent)
        except Exception as exc:  # not a git checkout, or git missing
            logger.warning("repo overlay: could not read the committed src/ "
                           "(%s) — falling back to the working tree (DEV-654)",
                           exc)
            if overlay_src.exists():
                shutil.rmtree(overlay_src)
            shutil.copytree(repo_src, overlay_src,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            sha, dirty = _src_tree_state(_SERVER_REPO_ROOT)
            if dirty:
                logger.warning(
                    "repo overlay built from HEAD %s; the working tree has %d "
                    "uncommitted change(s) under src/ that the sandbox will "
                    "NOT see: %s (DEV-654)", sha, len(dirty),
                    ", ".join(dirty[:6]) + (" …" if len(dirty) > 6 else ""))
            else:
                logger.info("repo overlay built from HEAD %s (working tree clean)", sha)
    workspace_src = spec_dir / "src"
    if workspace_src.is_dir():
        for src_file in sorted(workspace_src.rglob("*")):
            if not src_file.is_file():
                continue
            dest = overlay_src / src_file.relative_to(workspace_src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
    logger.info("repo overlay materialized for self-target spec: %s", overlay_src)
    return overlay_src


# ── DEV-675: existing tests that import an edited module ────────────────────
#
# The self-target pre-gate check ran the spec's NEW test files and nothing
# else, so an artifact that reverted merged behaviour reached the code-review
# gate under "compiled and the suite passed" — run 32 reverted DEV-672's
# `repo_relative()` with 8/8 green, and only a hand diff at the gate caught
# it. The repository's own tests are the cheapest reviewer there is: the ones
# that import an edited module run alongside the new ones, in ONE pytest
# invocation, and the output says which set each result belongs to.
EXISTING_TESTS_MODE_ENV = "AUTONOMOUS_SELF_TARGET_EXISTING_TESTS"
EXISTING_TESTS_MODES = ("imports", "all", "off")
EXISTING_TESTS_MARKER = "[self-target existing tests]"
_EXISTING_TESTS_HEADER_RE = re.compile(
    re.escape(EXISTING_TESTS_MARKER) + r" mode=(?P<mode>\w+) selected=(?P<n>\d+)")
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
# pytest -v result lines ("tests/test_x.py::test_a PASSED") and the short
# summary ("FAILED tests/test_x.py::test_a - AssertionError").
_PYTEST_VERBOSE_RE = re.compile(
    r"^(?P<id>\S+\.py::\S+)\s+(?P<res>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", re.M)
_PYTEST_SHORT_RE = re.compile(r"^(?P<res>FAILED|ERROR)\s+(?P<id>\S+\.py::\S+)", re.M)


def existing_tests_mode() -> str:
    """imports (default): run the repository tests that import an edited
    module; all: the whole tests/ tree; off: only the spec's own tests."""
    mode = (os.getenv(EXISTING_TESTS_MODE_ENV, "imports") or "imports").strip().lower()
    if mode not in EXISTING_TESTS_MODES:
        logger.warning("%s=%r is not one of %s — using 'imports' (DEV-675)",
                       EXISTING_TESTS_MODE_ENV, mode, "/".join(EXISTING_TESTS_MODES))
        return "imports"
    return mode


def edited_modules(spec_dir: Path) -> list[str]:
    """Dotted names of the workspace's non-test modules under src/.

    `src/coding_model_autonomous/outcome.py` → `coding_model_autonomous.outcome`;
    a package `__init__.py` names the package itself. Test files are the
    spec's own suite, not modules anything imports.
    """
    ws = spec_dir / _WORKSPACE_SRC_DIR
    if not ws.is_dir():
        return []
    out: list[str] = []
    for path in sorted(ws.rglob("*.py")):
        rel = path.relative_to(ws).as_posix()
        # Everything under src/ is a module, including one whose name happens
        # to match `test_*.py` — this repository ships `test_runner.py`
        # (DEV-688). Only a tests/ subtree inside the package is test code.
        if "tests/" in rel:
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            out.append(".".join(parts))
    return out


def test_imports_module(text: str, module: str) -> bool:
    """Does a test module import *module* directly?

    Matches `from pkg.mod import …`, `from pkg.mod.sub import …`,
    `import pkg.mod` / `import pkg.mod as m`, and `from pkg import mod`
    (bare, aliased, or inside a parenthesised list). Indirect imports — a
    helper that imports the module — are not followed; `all` mode is the
    answer when that matters.
    """
    esc = re.escape(module)
    if re.search(rf"^\s*from\s+{esc}(?:[.\s]|$)", text, re.M):
        return True
    if re.search(rf"^\s*import\s+{esc}(?:[.\s,]|$)", text, re.M):
        return True
    pkg, _, name = module.rpartition(".")
    if not pkg:
        return False
    for m in re.finditer(rf"^\s*from\s+{re.escape(pkg)}\s+import\s+"
                         rf"(?:\(([^)]*)\)|([^\n]*))", text, re.M):
        names = re.findall(r"\b\w+\b", m.group(1) or m.group(2) or "")
        if name in names:
            return True
    return False


def existing_tests_importing(edited: Iterable[str], tests_root: Path,
                             exclude: Collection[str] = ()) -> list[str]:
    """Test files under *tests_root* that import any module in *edited*.

    Returns paths relative to tests_root's parent (`tests/test_x.py`), so
    they compare directly with workspace paths. *exclude* names such paths
    the caller will run from the workspace instead — a test file the spec
    itself modified must not also run in its base_ref version.
    """
    modules = [m for m in edited if m]
    if not modules or not tests_root.is_dir():
        return []
    excluded = {str(e).strip().lstrip("./") for e in exclude}
    selected: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        rel = path.relative_to(tests_root.parent).as_posix()
        if not _TEST_FILE_RE.search(rel) or rel in excluded:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if NOT_IN_SANDBOX_MARKER in text:
            logger.info("existing tests: %s declares %s — not run inside the "
                        "sandbox (DEV-689)", rel, NOT_IN_SANDBOX_MARKER)
            continue
        if any(test_imports_module(text, m) for m in modules):
            selected.append(rel)
    return selected


def _all_tests_under(tests_root: Path, exclude: Collection[str] = ()) -> list[str]:
    excluded = {str(e).strip().lstrip("./") for e in exclude}
    out = []
    for path in sorted(tests_root.rglob("*.py")):
        rel = path.relative_to(tests_root.parent).as_posix()
        if not _TEST_FILE_RE.search(rel) or rel in excluded:
            continue
        try:
            if NOT_IN_SANDBOX_MARKER in path.read_text(encoding="utf-8",
                                                       errors="replace"):
                continue   # DEV-689
        except OSError:
            continue
        out.append(rel)
    return out


def _collection_targets(spec_dir: Path) -> list[str]:
    """spec_dir's top-level entries as explicit pytest arguments — the set
    `pytest <spec_dir>` would walk: no dot-directories, none of the dirs the
    run ignores, not `src/`, and top-level .py files.

    `src/` is excluded because it holds the attempt's SOURCE, which the
    overlay puts on PYTHONPATH for the tests to import — it is never a test
    target. Collecting it is not merely wasteful: a source file whose name
    matches `test_*.py` (this repository has `test_runner.py`) is imported as
    a test module under a synthesized `src.<pkg>` namespace package, its own
    relative imports fail, and every attempt reds identically at collection
    with a failure no model can fix (DEV-688).
    """
    out = []
    for entry in sorted(spec_dir.iterdir()):
        if entry.name.startswith(".") or entry.name in _SPEC_SKIP_PATTERNS:
            continue
        if entry.name == _WORKSPACE_SRC_DIR:
            continue
        if entry.is_dir() or entry.suffix == ".py":
            out.append(str(entry))
    return out


def _workspace_test_files(spec_dir: Path) -> list[str]:
    """Test files the spec itself wrote or modified (workspace-relative)."""
    out = []
    for path in sorted(spec_dir.rglob("*.py")):
        rel = path.relative_to(spec_dir).as_posix()
        if any(skip in rel.split("/") for skip in _SPEC_SKIP_PATTERNS):
            continue
        if _TEST_FILE_RE.search(rel):
            out.append(rel)
    return out


def _extract_committed_tree(repo_root: Path, into: Path,
                            skip: tuple[str, ...] = ("src/",)) -> None:
    """`git archive HEAD` extracted under *into*, minus the members under
    *skip* (src/ is the DEV-626 overlay's business). Raises on failure."""
    import io
    import tarfile
    out = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar",
                          "HEAD"], capture_output=True, check=True, timeout=60)
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
        members = [m for m in tar.getmembers()
                   if not any(m.name.startswith(s) for s in skip)]
        tar.extractall(path=into, members=members, filter="data")


def _copy_working_tree(repo_root: Path, into: Path,
                       skip: tuple[str, ...] = ("src/",)) -> None:
    """The tracked files as the working tree has them, minus *skip* — the
    AUTONOMOUS_OVERLAY_FROM_WORKING_TREE=1 opt-in's version of the above."""
    listed = subprocess.run(["git", "-C", str(repo_root), "ls-files", "-z"],
                            capture_output=True, check=True, timeout=60).stdout
    for rel in filter(None, listed.decode("utf-8", "replace").split("\0")):
        if any(rel.startswith(s) for s in skip):
            continue
        src = repo_root / rel
        if not src.is_file():
            continue
        dst = into / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _shadow_workspace_edits(spec_dir: Path, overlay_root: Path) -> list[str]:
    """Workspace files that edit a tracked file OUTSIDE src/ replace the
    committed copy in the overlay — the same rule the src/ overlay applies,
    so an existing test that reads `docs/PIPELINE.md` or `.env.example` sees
    the candidate's edit, not HEAD's. Only files that already exist in the
    overlay are shadowed: the workspace's own artifacts (design.md, plan.json,
    manifest.json …) are not repository files and stay out. Returns the
    shadowed paths."""
    shadowed: list[str] = []
    for top in sorted(spec_dir.iterdir()):
        if top.name in ("src", _REPO_OVERLAY_DIR, "retry_history") or \
                top.name in _SPEC_SKIP_PATTERNS or top.name.startswith("."):
            continue
        for path in ([top] if top.is_file() else sorted(top.rglob("*"))):
            if not path.is_file():
                continue
            rel = path.relative_to(spec_dir).as_posix()
            if any(skip in rel.split("/") for skip in _SPEC_SKIP_PATTERNS):
                continue
            target = overlay_root / rel
            if target.is_file():
                shutil.copy2(path, target)
                shadowed.append(rel)
    return shadowed


def _materialize_committed_tree(spec_dir: Path, overlay_root: Path) -> Optional[Path]:
    """The repository's committed tree — everything but src/, which the
    DEV-626 overlay already carries — under the overlay (DEV-675), with the
    workspace's edits to tracked files shadowing it.

    The whole tree, not just tests/: existing tests read the repository by
    path (`docs/PIPELINE.md` for the event-kind docs check, `.env.example`
    for knob coverage, README.md …), and a tests-only overlay reds them
    with FileNotFoundError on a correct attempt. Same source rule as the
    src/ overlay (DEV-654): the committed tree, the working tree only by
    the same explicit opt-in. Returns the tests dir, or None (with a
    warning) when the tree cannot be read.
    """
    if not (_SERVER_REPO_ROOT / "tests").is_dir():
        return None
    overlay_root.mkdir(parents=True, exist_ok=True)
    for entry in overlay_root.iterdir():
        if entry.name == "src":
            continue
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    try:
        if os.getenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", "") == "1":
            _copy_working_tree(_SERVER_REPO_ROOT, overlay_root)
        else:
            _extract_committed_tree(_SERVER_REPO_ROOT, overlay_root)
    except Exception as exc:
        logger.warning("repo overlay: could not read the committed tree (%s) "
                       "— existing tests will NOT run against this attempt "
                       "(DEV-675)", exc)
        return None
    shadowed = _shadow_workspace_edits(spec_dir, overlay_root)
    if shadowed:
        logger.info("repo overlay: %d workspace edit(s) outside src/ shadow the "
                    "committed copy: %s (DEV-675)", len(shadowed),
                    ", ".join(shadowed[:8]) + (" …" if len(shadowed) > 8 else ""))
    overlay_tests = overlay_root / "tests"
    return overlay_tests if overlay_tests.is_dir() else None


def select_existing_tests(spec_dir: Path, overlay_root: Path) -> tuple[str, list[str]]:
    """(mode, repo-relative test files) to run beside the spec's own tests."""
    mode = existing_tests_mode()
    if mode == "off":
        return mode, []
    tests_root = _materialize_committed_tree(spec_dir, overlay_root)
    if tests_root is None:
        return mode, []
    exclude = _workspace_test_files(spec_dir)
    if mode == "all":
        return mode, _all_tests_under(tests_root, exclude)
    return mode, existing_tests_importing(edited_modules(spec_dir), tests_root, exclude)


def existing_tests_header(mode: str, selected: list[str],
                          edited: Collection[str] = ()) -> str:
    """The line that heads the test output, naming what ran beside the new
    tests. Short on purpose: it shares the first 2000 chars of the output
    with the DEV-536 reconstruction marker."""
    shown = ", ".join(selected[:12]) + (f", … (+{len(selected) - 12} more)"
                                        if len(selected) > 12 else "")
    edited_note = (f" for edited {', '.join(list(edited)[:6])}"
                   if edited and mode == "imports" else "")
    return (f"{EXISTING_TESTS_MARKER} mode={mode} selected={len(selected)}"
            f"{edited_note}: {shown or 'none'}")


@dataclass
class TestSplit:
    """New-vs-existing results parsed from a self-target pytest run."""
    mode: str
    selected: int
    new_passed: int = 0
    new_failed: int = 0
    existing_passed: int = 0
    existing_failed: int = 0
    new_failed_ids: list[str] = field(default_factory=list)
    existing_failed_ids: list[str] = field(default_factory=list)

    @property
    def new_total(self) -> int:
        return self.new_passed + self.new_failed

    @property
    def existing_total(self) -> int:
        return self.existing_passed + self.existing_failed

    def payload(self) -> dict:
        return {"existing_tests_mode": self.mode,
                "existing_tests_selected": self.selected,
                "new_tests": {"passed": self.new_passed, "failed": self.new_failed},
                "existing_tests": {"passed": self.existing_passed,
                                   "failed": self.existing_failed},
                "existing_failed": self.existing_failed_ids[:20]}


def parse_test_split(output: str) -> Optional[TestSplit]:
    """Split a self-target run's results into the spec's tests and the
    repository's, by the overlay path in each node id. None when the run
    carried no DEV-675 header (a foreign repo, or a faked run)."""
    m = _EXISTING_TESTS_HEADER_RE.search(output or "")
    if m is None:
        return None
    split = TestSplit(mode=m.group("mode"), selected=int(m.group("n")))
    results: dict[str, str] = {}
    for hit in _PYTEST_VERBOSE_RE.finditer(output):
        results[hit.group("id")] = hit.group("res")
    for hit in _PYTEST_SHORT_RE.finditer(output):
        results[hit.group("id")] = hit.group("res")
    marker = _REPO_OVERLAY_DIR + "/"
    for node_id, res in results.items():
        existing = marker in node_id.split("::", 1)[0]
        if res in ("SKIPPED", "XFAIL"):
            continue
        failed = res in ("FAILED", "ERROR", "XPASS")
        if existing:
            if failed:
                split.existing_failed += 1
                split.existing_failed_ids.append(node_id)
            else:
                split.existing_passed += 1
        elif failed:
            split.new_failed += 1
            split.new_failed_ids.append(node_id)
        else:
            split.new_passed += 1
    return split


def _run_local_tests(spec_dir: Path, framework: str, timeout: int,
                     repo: Optional[str] = None) -> tuple[bool, str]:
    """Run pytest/jest/vitest/node_test locally (bwrap sandbox on Linux).

    LLM-generated test code runs inside a bubblewrap sandbox by default. If
    bwrap is unavailable, the test run fails with a clear diagnostic unless
    CODING_MODEL_ALLOW_UNSANDBOXED_TESTS is explicitly set.

    jest/vitest specs get a network-gated dependency-install phase first
    (_provision_node_modules); the test run itself is always offline.
    """
    if framework in NODE_MODULES_FRAMEWORKS:
        ok, install_output = _provision_node_modules(spec_dir)
        if not ok:
            return False, install_output

    extra_env: Optional[dict[str, str]] = None
    existing_header = ""
    if framework == "jest":
        # The local binary, not `npx jest`: npx would try to FETCH jest when
        # it isn't installed, and the test sandbox has no network, so that
        # fails with a confusing registry error instead of a plain
        # "jest is not installed" (DEV-104).
        #
        # Overriding --testPathIgnorePatterns REPLACES jest's defaults, so
        # /node_modules/ has to be restated alongside retry_history — without
        # it jest would collect the test files of every installed package.
        raw_cmd = [
            "node_modules/.bin/jest", "--no-coverage", "--ci",
            "--testPathIgnorePatterns", "/node_modules/", "/retry_history/",
        ]
    elif framework == "vitest":
        # `run` is mandatory — bare `vitest` starts a watch server and never
        # exits, which would burn the whole timeout and report a hang.
        # --exclude likewise replaces vitest's defaults, so node_modules is
        # restated for the same reason as jest above.
        raw_cmd = [
            "node_modules/.bin/vitest", "run",
            "--exclude", "**/node_modules/**", "--exclude", "**/retry_history/**",
        ]
    elif framework == "node_test":
        # Node's built-in test runner (node:test). Zero external deps and no
        # network: the sandbox provides `node` on PATH via the bound Node
        # toolchain (see _wrap_in_sandbox / SANDBOX_NODE_ROOT).
        #
        # We enumerate the test files EXPLICITLY rather than letting `node --test`
        # auto-discover from the cwd, so we can exclude `retry_history/` — the
        # snapshots of prior retries. This mirrors the pytest path's
        # `--ignore retry_history`. Without it, every retry's stale snapshot is
        # re-run and its historical failures poison the result, so a JS spec
        # could never pass once it had retried (killed spec_54b2c1b3 on
        # 2026-07-15: a fixed `node:assert` typo kept failing from retry_0's
        # snapshot). Fall back to auto-discovery only if we find no test files.
        test_files = sorted(
            p.relative_to(spec_dir).as_posix()
            for ext in ("js", "mjs", "cjs")
            for p in spec_dir.rglob(f"*.test.{ext}")
            if "retry_history" not in p.relative_to(spec_dir).parts
        )
        # Pin the TAP reporter explicitly. On Node >= 22 the default `--test`
        # reporter is `spec` even when stdout is piped (not a TTY), printing
        # `ℹ tests/pass/fail` instead of TAP's `# tests/pass/fail`. The
        # orchestrator's anti-hallucination guard (_NODE_TEST_SUMMARY_RE) only
        # recognises the TAP summary, so an unpinned reporter makes a genuinely
        # green run get force-failed as "no node:test summary detected". TAP is
        # what the structural validator — and every node_test test here —
        # expects, so make the pipeline reporter-default-independent.
        # An empty *test_files auto-discovers from cwd, preserving the fallback.
        raw_cmd = ["node", "--test", "--test-reporter=tap", *test_files]
    else:
        # `--import-mode=importlib`: import each test module by its full path
        # instead of pytest's default 'prepend' mode, which keys modules by
        # basename and inserts the test's dir on sys.path. Under 'prepend',
        # two test files that share a basename in different dirs (e.g.
        # `tests/test_spec.py` + `ParamountDemo/tests/test_spec.py`, or flat
        # vs nested across retries) collide and abort collection with
        # `import file mismatch` BEFORE any test runs — every retry then
        # FAILs on a harness artefact, not the code (killed spec_031e0aaa on
        # 2026-06-02). importlib imports by path, so duplicate basenames
        # coexist; it supersedes the fragile reviewer-test path normalization
        # as the real guard against this class of failure.
        #
        # `--ignore retry_history` still keeps pytest out of the
        # `_snapshot_retry` dirs so a retry's tests aren't double-collected
        # against the live ones (the snapshots feed the synthesis pass, not
        # a re-run).
        # `--ignore .repo_overlay` keeps pytest from double-collecting any
        # test modules that ship inside the copied repo tree (DEV-626).
        raw_cmd = [
            sys.executable, "-m", "pytest", "-v", "--tb=short",
            "--import-mode=importlib",
            "--ignore", str(spec_dir / "retry_history"),
            "--ignore", str(spec_dir / _REPO_OVERLAY_DIR),
            # DEV-688: and never the attempt's own source tree. This covers
            # the whole-directory target below; an --ignore does not override
            # an explicitly passed path, which is why _collection_targets
            # drops it as well.
            "--ignore", str(spec_dir / _WORKSPACE_SRC_DIR),
        ]
        targets = [str(spec_dir)]
        overlay_src = _materialize_local_repo_overlay(spec_dir, repo)
        if overlay_src is not None:
            extra_env = {"PYTHONPATH": str(overlay_src)}
            # DEV-675: the repository's own tests for the modules this
            # attempt edited run in the same invocation, as explicit paths.
            mode, selected = select_existing_tests(spec_dir, overlay_src.parent)
            if selected:
                # Named beside spec_dir's top-level entries, not beside
                # spec_dir itself: when the directory is an argument, pytest
                # meets `.repo_overlay` on that walk, never recurses into a
                # dot-directory, and then silently drops the explicit file
                # under it as already visited. The entries are what the
                # directory walk collected anyway. `--rootdir` keeps every
                # node id relative to the workspace, so the overlay prefix
                # is what tells the two sets apart.
                targets = _collection_targets(spec_dir) + [
                    str(overlay_src.parent / p) for p in selected]
                raw_cmd += ["--rootdir", str(spec_dir)]
                # Each selected file's own directory joins PYTHONPATH: that
                # is what pytest's default prepend mode does for a conftest's
                # sibling imports (tests/seams/conftest.py imports seam_fakes)
                # and importlib mode does not.
                test_dirs = sorted({str((overlay_src.parent / p).parent) for p in selected})
                extra_env["PYTHONPATH"] = os.pathsep.join([str(overlay_src), *test_dirs])
            existing_header = existing_tests_header(mode, selected,
                                                    edited_modules(spec_dir))
            logger.info("spec %s: %s (DEV-675)", spec_dir.name, existing_header)
        raw_cmd += targets

    # No share_net: the test run itself is always offline, for every
    # framework. Only _provision_node_modules above opens the network.
    passed, output = _run_confined(raw_cmd, spec_dir, timeout, what="tests",
                                   extra_env=extra_env)
    if existing_header:
        output = existing_header + "\n" + output
    return passed, output


def _collect_patch_files(spec_dir: Path) -> tuple[list[dict], Optional[str]]:
    """Enumerate spec_dir as UTF-8 patch files for the Mac runner.

    Returns (patch_files, error). On binary-content encounter, returns
    ([], error_message) so the caller can fail fast.
    """
    patch_files: list[dict] = []
    spec_root = spec_dir.resolve()
    for p in sorted(spec_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(spec_root).as_posix()
        if any(skip in rel.split("/") for skip in _SPEC_SKIP_PATTERNS):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return [], f"non-UTF8 file in spec: {rel} (binary patches not supported)"
        patch_files.append({"path": rel, "content": content})
    return patch_files, None


def _drop_protected(patch_files: list[dict],
                    protected_paths) -> tuple[list[dict], list[str]]:
    """Remove paths the spec puts off-limits, restoring them to the base.

    The runner materialises a git worktree at ``base_ref`` and then writes the
    patch files over it, so a path simply *not sent* keeps whatever the base
    commit has. That makes restoration free — no diff, no checkout, no
    round-trip — and it is why this is the cheap slice of DEV-400 that lands
    without waiting on attempt-branch transport.

    Returns (kept, dropped_paths).
    """
    if not protected_paths:
        return patch_files, []
    protected = {p.strip().lstrip("./") for p in protected_paths if p and p.strip()}
    if not protected:
        return patch_files, []
    kept, dropped = [], []
    for item in patch_files:
        if item["path"] in protected:
            dropped.append(item["path"])
        else:
            kept.append(item)
    return kept, dropped


# ── Transport failure, told apart from a test failure (DEV-538) ─────────────
#
# A dispatch that never reached the runner says nothing about the code, but it
# used to come back in the same `(False, str)` shape a genuine test failure
# does, leaving every caller to re-derive "this told us nothing" from the
# absence of two other things. This marker is the signal; is_runner_unreachable
# is how callers branch on it.
RUNNER_UNREACHABLE = "mac-runner unreachable"

# DEV-536: the runner's per-file overwrite record carries
# `suspected_reconstruction` (new_lines < old_lines * RUNNER_OVERWRITE_SHRINK_RATIO)
# and the orchestrator never read it, so the detector helped only someone
# reading the Mac's log by hand. It heads the output now — first, not last,
# as the runner does with its own integration warnings — so the reviewer, the
# retry feedback and the human at the gate all see it, and the daemon records
# it on the TEST_RAN row. Since DEV-492's read path removed the CAUSE, this is
# a regression detector: it is what says the read path has silently stopped
# working (a Mac not redeployed, a fetch failing soft, a table naming nothing).
RECONSTRUCTION_MARKER = "[suspected reconstruction]"


# Connection-level failures fail fast (the Mac is asleep, the tunnel is gone),
# so retrying is nearly free and covers DEV-518's link re-enumeration, which
# clears in seconds. A read timeout is the opposite: it has already waited the
# full window — 330s in run 8 — so a second attempt costs another one. Retry it
# once, in case the runner was mid-wake, and no more.
_DISPATCH_CONNECT_BACKOFFS = (5, 15)
_DISPATCH_READ_TIMEOUT_RETRIES = 1


def is_runner_unreachable(output: str) -> bool:
    """True when *output* is a transport failure rather than a test result."""
    return RUNNER_UNREACHABLE in (output or "")


def runner_version() -> "dict | None":
    """What the Mac runner is serving, or None when it cannot say (DEV-805).

    `mac_runner/*` deploys on the Mac alone, so a merge here changes nothing
    there until somebody pulls. Twice that has been invisible: DEV-705's
    timeout raise was inert for a day while the record called it fixed, and
    DEV-752's runner-side half could not be confirmed from this host at all.

    None on any failure, including an older runner with no such route — this
    is telemetry about the dispatch and must never be able to stop one.
    """
    try:
        resp = _SESSION.get(
            f"{MAC_RUNNER_URL.rstrip('/')}/v1/version",
            headers={"X-Runner-Key": MAC_RUNNER_API_KEY}, timeout=15,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _log_runner_version(framework: str, timeout: int) -> None:
    """One line per dispatch naming the runner's commit, and a warning when its
    timeout table disagrees with ours.

    The effective budget is the MINIMUM of the two tables, so a raise on one
    host alone is inert. Saying which value will actually apply costs one line
    and is the whole of what DEV-705 lost a day to.
    """
    info = runner_version()
    if info is None:
        logger.info("mac-runner version: unknown (no /v1/version route — the "
                    "runner predates DEV-805, so mac_runner fixes merged since "
                    "cannot be confirmed live from here)")
        return
    commit = (info.get("commit") or "unknown")[:12]
    dirty = " DIRTY" if info.get("dirty") else ""
    logger.info("mac-runner version: %s%s", commit, dirty)
    theirs = (info.get("timeouts") or {}).get(framework)
    ours = DEFAULT_TIMEOUTS.get(framework)
    if theirs is not None and ours is not None and theirs != ours:
        logger.warning(
            "mac-runner's %s default is %ds and ours is %ds — the effective "
            "budget is the minimum, so whichever table was raised alone is "
            "inert (DEV-705/DEV-805). This dispatch asks for %ds.",
            framework, theirs, ours, timeout)


def _dispatch_with_retry(url: str, payload: dict, headers: dict,
                         http_timeout: int):
    """POST to the runner, retrying transport failures with bounded backoff.

    Raises the last requests.RequestException if every attempt fails.
    """
    read_timeouts = 0
    connect_attempt = 0
    while True:
        try:
            return _SESSION.post(url, json=payload, headers=headers,
                                 timeout=http_timeout)
        except requests.Timeout as e:
            read_timeouts += 1
            if read_timeouts > _DISPATCH_READ_TIMEOUT_RETRIES:
                raise
            logger.warning("mac-runner read timeout after %ds — retrying once "
                           "in case the runner was waking (%s)",
                           http_timeout, e)
        except requests.ConnectionError as e:
            if connect_attempt >= len(_DISPATCH_CONNECT_BACKOFFS):
                raise
            delay = _DISPATCH_CONNECT_BACKOFFS[connect_attempt]
            connect_attempt += 1
            logger.warning("mac-runner unreachable (%s) — retry %d/%d in %ds",
                           type(e).__name__, connect_attempt,
                           len(_DISPATCH_CONNECT_BACKOFFS), delay)
            time.sleep(delay)


def _run_mac_runner_tests(
    spec_dir: Path,
    framework: str,
    timeout: int,
    *,
    repo: Optional[str],
    base_ref: str = "HEAD",
    scheme: Optional[str] = None,
    destination: Optional[str] = None,
    configuration: Optional[str] = None,
    workspace: Optional[str] = None,
    project: Optional[str] = None,
    filter: Optional[str] = None,
    skip_filter: Optional[str] = None,
    protected_paths: Optional[list] = None,
) -> tuple[bool, str]:
    """Dispatch swift_test / xcodebuild_test to the Mac runner over HTTP."""
    if not MAC_RUNNER_API_KEY:
        return False, (
            "MAC_RUNNER_API_KEY is not set on the orchestrator. Configure "
            "MAC_RUNNER_URL and MAC_RUNNER_API_KEY in ~/.config/coding-model-server/.env "
            "to dispatch Swift/Xcode tests to the Mac runner."
        )
    if not repo:
        return False, (
            f"{framework} requires a 'repo' (symbolic name registered in the "
            f"Mac runner's repos.yml). Add it to the spec's test_strategy block."
        )

    patch_files, err = _collect_patch_files(spec_dir)
    if err:
        return False, err

    # DEV-427: files the spec declares off-limits are never sent, so the
    # worktree keeps the base_ref version of each.
    patch_files, dropped_protected = _drop_protected(patch_files, protected_paths)
    if dropped_protected:
        logger.warning("restoring %d protected path(s) to %s: %s",
                       len(dropped_protected), base_ref,
                       ", ".join(dropped_protected))

    payload: dict = {
        "spec_id": spec_dir.name,
        "repo": repo,
        "base_ref": base_ref,
        "patch_files": patch_files,
        "framework": framework,
        "timeout": timeout,
    }
    for key, val in (("scheme", scheme), ("destination", destination),
                     ("configuration", configuration), ("workspace", workspace),
                     ("project", project), ("filter", filter),
                     ("skip_filter", skip_filter)):
        if val is not None:
            payload[key] = val

    url = f"{MAC_RUNNER_URL.rstrip('/')}/v1/run_tests"
    headers = {"X-Runner-Key": MAC_RUNNER_API_KEY}
    _log_runner_version(framework, timeout)
    # Give the HTTP call headroom beyond the test timeout so the runner can
    # finish packaging the response even on a long run.
    http_timeout = timeout + 30

    logger.info("dispatching %s to mac-runner %s (timeout=%ds, %d files)",
                framework, url, timeout, len(patch_files))
    try:
        resp = _dispatch_with_retry(url, payload, headers, http_timeout)
    except requests.RequestException as e:
        return False, f"{RUNNER_UNREACHABLE} at {url}: {e}"

    if resp.status_code != 200:
        return False, f"mac-runner HTTP {resp.status_code}: {resp.text[:2000]}"

    try:
        data = resp.json()
    except ValueError:
        return False, f"mac-runner returned non-JSON response: {resp.text[:2000]}"

    output = str(data.get("output", ""))
    reconstructed = [ow for ow in (data.get("overwrites") or [])
                     if isinstance(ow, dict) and ow.get("suspected_reconstruction")]
    if reconstructed:
        output = (
            f"{RECONSTRUCTION_MARKER} {len(reconstructed)} existing file(s) "
            "were REPLACED by a much smaller version. A file the implementer "
            "never read, re-emitted from imagination (DEV-492): an edit does "
            "not shrink a file like this. Treat the shrunken file as suspect "
            "before anything else in this output:\n"
            + "".join(f"  - {ow.get('path')}: {ow.get('old_lines')} -> "
                      f"{ow.get('new_lines')} lines\n" for ow in reconstructed)
            + "\n" + output
        )
    if dropped_protected:
        # Say so in the output the reviewer and the retry both read: silently
        # discarding the implementer's version of a file would be its own
        # surprise.
        output = (
            "[protected paths] the implementer modified "
            f"{len(dropped_protected)} off-limits file(s); each was restored "
            f"to {base_ref} and its version was NOT used:\n"
            + "".join(f"  - {p}\n" for p in dropped_protected)
            + "\n" + output
        )
    return bool(data.get("passed")), output


def _is_self_target_repo(repo: str) -> bool:
    """True when *repo* names this very repository and it is a git checkout."""
    return bool(repo) and repo == _SERVER_REPO_ROOT.name and \
        (_SERVER_REPO_ROOT / ".git").exists()


def _local_head(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=False).stdout.strip() or "?"
    except OSError:
        return "?"


def _reject_unsafe_read_path(rel: str) -> Optional[str]:
    """Mirror of the runner's check: `git show ref:path` cannot leave the
    tree anyway, but a clear refusal beats a confusing git error."""
    if not rel or not rel.strip():
        return "empty path"
    if rel.startswith("/"):
        return "absolute paths are not accepted"
    if ".." in Path(rel).parts:
        return "path escapes the repository"
    return None


_LOCAL_READ_PER_FILE_MAX_BYTES = 2_000_000


def _read_local_repo_files(root: Path, paths: list[str], base_ref: str,
                           ) -> tuple[list[tuple[str, str]], list[str]]:
    """`git show <base_ref>:<path>` in *root* for every path — files as their
    text, directories as git's own `tree <ref>:<dir>/` listing — with the same
    in-band per-path problems the runner returns (DEV-674)."""
    files: list[tuple[str, str]] = []
    problems: list[str] = []
    for rel in paths:
        err = _reject_unsafe_read_path(rel)
        if err:
            problems.append(f"{rel}: {err}")
            continue
        try:
            proc = subprocess.run(["git", "-C", str(root), "show", f"{base_ref}:{rel}"],
                                  capture_output=True, check=False)
        except OSError as e:
            problems.append(f"{rel}: git unavailable: {e}")
            continue
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            problems.append(f"{rel}: {(detail or 'git show failed')[:300]}")
            continue
        if len(proc.stdout) > _LOCAL_READ_PER_FILE_MAX_BYTES:
            problems.append(f"{rel}: file too large ({len(proc.stdout)} bytes)")
            continue
        files.append((rel, proc.stdout.decode("utf-8", "replace")))
    return files, problems


def fetch_repo_files(
    repo: str,
    paths: list[str],
    base_ref: str = "HEAD",
    timeout: int = 30,
    ref_state: Optional[dict] = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Read *paths* from the Mac runner at *base_ref* (DEV-492).

    Returns ``(files, problems)`` — files as (path, content) pairs, problems as
    human-readable strings naming what could not be read.

    **Fails soft by design.** If the runner is unreachable, or is an older
    build with no ``/v1/read_files`` route, this returns no files and one
    problem rather than raising. The implementer then runs exactly as it did
    before this feature existed: worse, but not broken. That property is what
    lets the two hosts be deployed in either order, and keeps a Mac restart
    mid-flight from failing a spec.

    The timeout is short on purpose. This is a git read, not a build — if it
    has not answered in 30s the link is in trouble, and stalling the whole
    execution pass behind it buys nothing.

    *ref_state*, when given, is FILLED IN with which commit the read was
    actually served from and whether the serving clone is current (DEV-701).
    A caller-owned dict rather than a third return value, because this is
    telemetry riding along with the answer and every existing caller unpacks
    a 2-tuple. Deliberately NOT folded into *problems*: a stale clone is not
    a per-path failure, and adding an unprefixed entry there would make
    :func:`problems_indicate_runner_outage` read a healthy runner as an
    outage.

    An older runner returns no ``ref_state`` at all, in which case the dict
    is left with ``in_sync`` absent — unknown, which is not the same as clear.
    """
    if not paths:
        return [], []
    if _is_self_target_repo(repo):
        # DEV-674: a self-target read must come from the tree the sandbox
        # overlay is built from — this repository's own HEAD — never from the
        # Mac's clone of it, which is at whatever commit it was last pulled to.
        # Run 32 edited a pre-DEV-672 outcome.py fetched from the Mac and its
        # artifact reverted the merged fix with every new test green.
        local_files, local_problems = _read_local_repo_files(_SERVER_REPO_ROOT, paths, base_ref)
        head = _local_head(_SERVER_REPO_ROOT)
        if ref_state is not None:
            # A self-target read is served from this repository's own working
            # tree, so it cannot lag a remote the way the Mac's clone can.
            ref_state.update({"ref": base_ref, "local_sha": head,
                              "source": "local", "in_sync": True})
        logger.info("read_files: repo=%s source=local sha=%s ref=%s requested=%d got=%d problems=%d",
                    repo, head, base_ref, len(paths),
                    len(local_files), len(local_problems))
        return local_files, local_problems
    url = f"{MAC_RUNNER_URL.rstrip('/')}/v1/read_files"
    payload = {"repo": repo, "base_ref": base_ref, "paths": list(paths)}
    try:
        resp = _SESSION.post(
            url, json=payload,
            headers={"X-Runner-Key": MAC_RUNNER_API_KEY}, timeout=timeout,
        )
    except requests.RequestException as e:
        problem = f"could not reach the runner's read path: {e}"
        logger.warning("read_files: repo=%s ref=%s requested=%d FAILED — %s",
                       repo, base_ref, len(paths), problem)
        return [], [problem]
    # Every early return below is a whole-fetch failure, and each one used to
    # be silent: the caller folds it into `problems` and the pass continues
    # with less context than it asked for. 747 auth rejections accumulated
    # this way with no line in either host's log. Log before returning, so the
    # count is recoverable from the journal rather than only from the runner's
    # access log.
    if resp.status_code == 404:
        problem = "runner has no /v1/read_files route (needs redeploy)"
        logger.warning("read_files: repo=%s ref=%s requested=%d FAILED — %s",
                       repo, base_ref, len(paths), problem)
        return [], [problem]
    if resp.status_code != 200:
        problem = f"read_files HTTP {resp.status_code}: {resp.text[:300]}"
        hint = ""
        if resp.status_code == 401:
            hint = (" — the runner rejected this host's key; check that "
                    "MAC_RUNNER_API_KEY here matches CODING_MODEL_RUNNER_API_KEY "
                    "on the Mac, and that no second mac_runner.server is "
                    "answering this port locally")
        logger.warning("read_files: repo=%s ref=%s requested=%d FAILED — %s%s",
                       repo, base_ref, len(paths), problem, hint)
        return [], [problem]
    try:
        data = resp.json()
    except ValueError:
        return [], ["read_files returned a non-JSON response"]

    files: list[tuple[str, str]] = []
    problems: list[str] = []
    for item in data.get("files") or []:
        path = str(item.get("path", ""))
        content = item.get("content")
        if content is None:
            problems.append(f"{path}: {item.get('error') or 'unreadable'}")
        else:
            files.append((path, content))
    state = data.get("ref_state")
    if isinstance(state, dict):
        if ref_state is not None:
            ref_state.update(state)
            ref_state.setdefault("source", "runner")
        if state.get("in_sync") is False:
            # DEV-701: run 39 built a whole Centipede slice on a clone that
            # predated the previous one. Every stage passed; the branch only
            # read as destructive when compared against origin, after the
            # compute was spent. This is the warning that was missing.
            logger.warning(
                "read_files: STALE CLONE serving repo=%s — %s",
                repo, state.get("note") or "local ref is not the remote ref")
        elif state.get("in_sync") is None:
            logger.info("read_files: repo=%s staleness UNKNOWN (%s)",
                        repo, state.get("note") or "no remote comparison")
    logger.info("read_files: repo=%s ref=%s sha=%s requested=%d got=%d problems=%d",
                repo, base_ref,
                str((state or {}).get("local_sha") or "?")[:12] if isinstance(state, dict) else "?",
                len(paths), len(files), len(problems))
    return files, problems


def problems_indicate_runner_outage(
    problems: list[str], requested_paths: list[str],
) -> bool:
    """True when a fetch_repo_files problem list means the WHOLE fetch failed
    (DEV-620): the transport-class early returns above each produce exactly one
    problem that is not prefixed by any requested path. Per-path problems
    ("<path>: not found") mean the runner answered — those files are simply
    being created, which is not an outage."""
    if len(problems) != 1:
        return False
    return not any(problems[0].startswith(f"{p}:") for p in requested_paths)


def run_tests(
    spec_dir: Path,
    framework: str = "pytest",
    timeout: Optional[int] = None,
    **framework_opts,
) -> tuple[bool, str]:
    """Run tests for a spec.

    Dispatches by framework:
      - pytest / python / node_test → local (bwrap sandbox on Linux)
      - jest / vitest            → local, preceded by a network-gated
                                   dependency-install phase (DEV-104)
      - swift_test               → Mac runner HTTP dispatch; requires `repo`
      - xcodebuild_test          → Mac runner HTTP dispatch; requires `repo` + `scheme`

    framework_opts carries the framework-specific configuration from the
    planner's test_strategy block (repo, base_ref, scheme, destination,
    configuration, workspace, project, filter, protected_paths) — unknown
    keys are ignored.

    protected_paths (Apple frameworks only) names repo-relative files the
    spec puts off-limits. They are dropped from the dispatch payload, so the
    worktree keeps the base_ref version of each (DEV-427).

    Returns (passed, combined_output).
    """
    framework = _APPLE_FRAMEWORK_ALIASES.get(framework, framework)

    # An unrecognised framework used to fall through to the local branch, whose
    # own else-arm is pytest. A plan asking for Apple tests therefore ran pytest
    # against whatever happened to be in the spec dir and reported the result as
    # authoritative — a placeholder `def test_stub(): pass` scored a PASS and a
    # spec with only XCTest files scored "no tests ran" (DEV-392). Refuse instead.
    if framework not in KNOWN_FRAMEWORKS:
        return False, (
            f"unknown test framework {framework!r}. Known frameworks: "
            f"{', '.join(sorted(KNOWN_FRAMEWORKS))}. Apple targets must use "
            f"'xcodebuild_test' (app/scheme) or 'swift_test' (SwiftPM); refusing "
            f"to fall back to a local runner, which would test the wrong thing."
        )

    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUTS.get(framework, 120)

    if framework in ("swift_test", "xcodebuild_test"):
        passed, output = _run_mac_runner_tests(
            spec_dir, framework, effective_timeout,
            repo=framework_opts.get("repo"),
            base_ref=framework_opts.get("base_ref", "HEAD"),
            scheme=framework_opts.get("scheme"),
            destination=framework_opts.get("destination"),
            configuration=framework_opts.get("configuration"),
            workspace=framework_opts.get("workspace"),
            project=framework_opts.get("project"),
            filter=framework_opts.get("filter"),
            # DEV-713: the operator's quarantine for a known flake (DEV-603).
            skip_filter=framework_opts.get("skip_filter"),
            protected_paths=framework_opts.get("protected_paths"),
        )
    else:
        passed, output = _run_local_tests(spec_dir, framework, effective_timeout,
                                          repo=framework_opts.get("repo"))

    logger.info("test result: %s (%d chars output)",
                "PASS" if passed else "FAIL", len(output))
    return passed, (output or "").strip()


# ── DEV-700: count test declarations per framework ───────────────────────────


def count_test_declarations(source: str, framework: str) -> int:
    """Count test declarations in *source* for the given *framework*.

    Returns an integer count of test function declarations found. This is a
    pure helper that does not perform any I/O and has no side effects.

    Frameworks supported:
      - "pytest": counts lines matching `def test_...(` pattern (indented or
        top-level), excluding commented-out lines (`# def test_x(`).
      - "swift_test": counts both XCTest (`func testFoo(`) and swift-testing
        (`@Test` attribute line followed by a function declaration).

    Unknown frameworks or empty/whitespace-only sources return 0.
    """
    source = source.strip()
    if not source:
        return 0

    # Normalize framework name using existing aliases
    normalized_framework = _APPLE_FRAMEWORK_ALIASES.get(framework.lower(), framework.lower())

    if normalized_framework == "pytest":
        return _count_pytest_tests(source)
    elif normalized_framework in ("swift_test", "xcodebuild_test"):
        return _count_swift_tests(source)
    else:
        return 0


_PYTEST_TEST_RE = re.compile(r'^\s*def\s+test_\w+\s*\(')


def _count_pytest_tests(source: str) -> int:
    """Count pytest-style test functions in Python source."""
    count = 0
    in_multiline_string = False
    
    for line in source.splitlines():
        stripped = line.lstrip()
        
        # Skip comment lines entirely
        if stripped.startswith('#'):
            continue
        
        # Handle triple-quote state tracking
        if '"""' in line or "'''" in line:
            # Count occurrences of triple quotes on this line
            double_quotes = line.count('"""')
            single_quotes = line.count("'''")
            
            # If odd number of triple quotes, toggle state
            total_triple_quotes = double_quotes + single_quotes
            if total_triple_quotes % 2 == 1:
                in_multiline_string = not in_multiline_string
            
            # If we're now inside a multiline string, skip this line
            if in_multiline_string:
                continue
        
        # Skip if currently inside a multiline string
        if in_multiline_string:
            continue
        
        # Check for pytest test pattern
        if _PYTEST_TEST_RE.match(line):
            count += 1
    
    return count


# DEV-751: XCTest discovers ANY method whose name begins with `test` —
# `testFoo` and `test_foo` both run. The former `test[A-Z]` form read run 44's
# twelve snake_case tests as zero and under-counted 37 of 386 archived files.
_SWIFT_FUNC_TEST_RE = re.compile(r'\bfunc\s+test\w*\s*\(')
_SWIFT_ATTRIBUTE_RE = re.compile(r'^\s*@Test\b')


def _count_swift_tests(source: str) -> int:
    """Count Swift test functions (XCTest and swift-testing).

    Counts @Test attribute lines immediately without lookahead. Each @Test
    contributes exactly 1. Also counts func testFoo( declarations separately.
    A line matching both (@Test func testFoo()) counts as ONE, not two.
    """
    count = 0
    lines = source.splitlines()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Skip comment lines
        stripped = line.lstrip()
        if stripped.startswith('//'):
            i += 1
            continue
        
        # Check for @Test attribute - count it immediately on its own line
        if _SWIFT_ATTRIBUTE_RE.match(line):
            count += 1
            i += 1
            continue
        
        # Check for XCTest-style func testFoo( pattern
        if _SWIFT_FUNC_TEST_RE.search(line):
            count += 1
        
        i += 1
    
    return count


def declaration_delta(
    before: dict[str, str],
    after: dict[str, str],
    framework: str,
) -> dict[str, int]:
    """Compute per-file delta of test declarations between two snapshots.

    Takes two dictionaries mapping file paths to source text (before/after),
    counts test declarations using *framework*'s rules, and returns a dictionary
    mapping only those paths where the count changed. A path present in `after`
    but not `before` contributes its full count; unchanged paths are absent.

    This function is pure — it does not mutate inputs and performs no I/O.
    """
    result: dict[str, int] = {}
    
    all_paths = set(before.keys()) | set(after.keys())
    
    for path in all_paths:
        before_count = count_test_declarations(before.get(path, ""), framework)
        after_count = count_test_declarations(after.get(path, ""), framework)
        
        delta = after_count - before_count
        if delta != 0:
            result[path] = delta
    
    return result
