"""Test execution for autonomous specs: sandboxing and dispatch.

Extracted verbatim from executor.py (DEV-152). This is the security-relevant
layer — bubblewrap + seccomp confinement of LLM-generated tests, and the
local-vs-mac-runner dispatch — and it was previously unreachable for unit
testing without importing the whole 2,300-line executor (and its import-time
load_dotenv/basicConfig). It depends on nothing in executor but the shared
HTTP session.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys

import requests
from pathlib import Path
from typing import Optional

from . import seccomp_filter
from .executor import _SESSION

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


def _wrap_in_sandbox(
    cmd: list[str],
    spec_dir: Path,
    seccomp_fd: Optional[int] = None,
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
        "--bind", str(spec_abs), str(spec_abs),
        "--chdir", str(spec_abs),
    ]
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
    "jest": 120,
    "node_test": 120,
    "swift_test": 300,
    "xcodebuild_test": 900,
}

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


def _run_local_tests(spec_dir: Path, framework: str, timeout: int) -> tuple[bool, str]:
    """Run pytest/jest/node_test locally (bwrap sandbox on Linux).

    LLM-generated test code runs inside a bubblewrap sandbox by default. If
    bwrap is unavailable, the test run fails with a clear diagnostic unless
    CODING_MODEL_ALLOW_UNSANDBOXED_TESTS is explicitly set.
    """
    if framework == "jest":
        raw_cmd = ["npx", "jest", "--no-coverage", "--roots", str(spec_dir)]
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
        raw_cmd = ["node", "--test", *test_files] if test_files else ["node", "--test"]
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
            "running LLM-generated tests WITHOUT a sandbox — tests have full "
            "access to this user's environment"
        )
    elif _sandbox_available():
        bpf_fd = seccomp_filter.build_seccomp_bpf_fd()
        cmd = _wrap_in_sandbox(raw_cmd, spec_dir, seccomp_fd=bpf_fd)
        if bpf_fd is None:
            sandbox_mode = "bwrap (no seccomp — libseccomp unavailable)"
            logger.warning(
                "seccomp filter unavailable; bwrap will run without --seccomp. "
                "Install python3-seccomp on the host to enable kernel-syscall "
                "filtering for LLM-generated tests."
            )
        else:
            sandbox_mode = "bwrap+seccomp"
    else:
        msg = (
            "Refusing to run LLM-generated tests: bwrap (bubblewrap) is not "
            "available and CODING_MODEL_ALLOW_UNSANDBOXED_TESTS is not set. Install "
            "bubblewrap (e.g. `apt install bubblewrap` on Debian/Ubuntu) on "
            "the Linux server, or set CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1 to opt "
            "out (not recommended — tests run with the orchestrator's own "
            "privileges)."
        )
        logger.error(msg)
        return False, msg

    logger.info("running tests via %s: %s (timeout=%ds)",
                sandbox_mode, " ".join(raw_cmd), timeout)

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
            passed = proc.returncode == 0
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
                f"Tests timed out after {timeout}s\n"
                f"--- partial stdout ---\n{_truncate(out or '')}\n"
                f"--- partial stderr ---\n{_truncate(err or '')}"
            )
            passed = False
    except Exception as e:
        output = f"Test runner failed: {type(e).__name__}: {e}"
        passed = False
    finally:
        if bpf_fd is not None:
            try:
                os.close(bpf_fd)
            except OSError:
                pass

    return passed, output.strip()


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
        resp = _SESSION.post(url, json=payload, headers=headers, timeout=http_timeout)
    except requests.RequestException as e:
        return False, f"mac-runner unreachable at {url}: {e}"

    if resp.status_code != 200:
        return False, f"mac-runner HTTP {resp.status_code}: {resp.text[:2000]}"

    try:
        data = resp.json()
    except ValueError:
        return False, f"mac-runner returned non-JSON response: {resp.text[:2000]}"

    return bool(data.get("passed")), str(data.get("output", ""))


def run_tests(
    spec_dir: Path,
    framework: str = "pytest",
    timeout: Optional[int] = None,
    **framework_opts,
) -> tuple[bool, str]:
    """Run tests for a spec.

    Dispatches by framework:
      - pytest / python / jest / node_test → local (bwrap sandbox on Linux)
      - swift_test               → Mac runner HTTP dispatch; requires `repo`
      - xcodebuild_test          → Mac runner HTTP dispatch; requires `repo` + `scheme`

    framework_opts carries the framework-specific configuration from the
    planner's test_strategy block (repo, base_ref, scheme, destination,
    configuration, workspace, project, filter) — unknown keys are ignored.

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
        )
    else:
        passed, output = _run_local_tests(spec_dir, framework, effective_timeout)

    logger.info("test result: %s (%d chars output)",
                "PASS" if passed else "FAIL", len(output))
    return passed, (output or "").strip()


