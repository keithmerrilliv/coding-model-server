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
import shutil
import signal
import subprocess
import sys
import time

import requests
from pathlib import Path
from typing import Optional

from . import seccomp_filter

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
DEFAULT_TIMEOUTS: dict[str, int] = {
    "pytest": 120,
    "python": 120,
    "jest": 180,
    "vitest": 180,
    "node_test": 120,
    "swift_test": 300,
    "xcodebuild_test": 900,
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
_SPEC_SKIP_PATTERNS = (".pytest_cache", "__pycache__", ".DS_Store",
                       "test_output.txt", "retry_history")


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
) -> tuple[bool, str]:
    """Run *raw_cmd* under bwrap+seccomp; return (exited_zero, combined_output).

    Extracted from _run_local_tests (DEV-104) so the dependency-install phase
    gets the identical confinement and process handling — the killpg-on-timeout
    and output cap below matter at least as much for `npm install`, which
    happily spawns a tree of children and can emit unbounded progress output.

    *share_net* is forwarded to _wrap_in_sandbox; only the install phase sets
    it. *what* names the activity for logs and the bwrap-missing diagnostic.
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
        cmd = _wrap_in_sandbox(raw_cmd, spec_dir, seccomp_fd=bpf_fd,
                               share_net=share_net, extra_binds=extra_binds)
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


def _run_local_tests(spec_dir: Path, framework: str, timeout: int) -> tuple[bool, str]:
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
        raw_cmd = [
            sys.executable, "-m", "pytest", "-v", "--tb=short",
            "--import-mode=importlib",
            "--ignore", str(spec_dir / "retry_history"),
            str(spec_dir),
        ]

    # No share_net: the test run itself is always offline, for every
    # framework. Only _provision_node_modules above opens the network.
    return _run_confined(raw_cmd, spec_dir, timeout, what="tests")


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
                     ("project", project), ("filter", filter)):
        if val is not None:
            payload[key] = val

    url = f"{MAC_RUNNER_URL.rstrip('/')}/v1/run_tests"
    headers = {"X-Runner-Key": MAC_RUNNER_API_KEY}
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


def fetch_repo_files(
    repo: str,
    paths: list[str],
    base_ref: str = "HEAD",
    timeout: int = 30,
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
    """
    if not paths:
        return [], []
    url = f"{MAC_RUNNER_URL.rstrip('/')}/v1/read_files"
    payload = {"repo": repo, "base_ref": base_ref, "paths": list(paths)}
    try:
        resp = _SESSION.post(
            url, json=payload,
            headers={"X-Runner-Key": MAC_RUNNER_API_KEY}, timeout=timeout,
        )
    except requests.RequestException as e:
        return [], [f"could not reach the runner's read path: {e}"]
    if resp.status_code == 404:
        return [], ["runner has no /v1/read_files route (needs redeploy)"]
    if resp.status_code != 200:
        return [], [f"read_files HTTP {resp.status_code}: {resp.text[:300]}"]
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
    logger.info("read_files: repo=%s ref=%s requested=%d got=%d problems=%d",
                repo, base_ref, len(paths), len(files), len(problems))
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
            protected_paths=framework_opts.get("protected_paths"),
        )
    else:
        passed, output = _run_local_tests(spec_dir, framework, effective_timeout)

    logger.info("test result: %s (%d chars output)",
                "PASS" if passed else "FAIL", len(output))
    return passed, (output or "").strip()


