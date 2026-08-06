"""FastAPI endpoint for the Mac-side test runner.

Runs on the macOS workstation. Accepts JSON payloads from the Linux
orchestrator (over an SSH tunnel to 127.0.0.1:5050), materializes a git
worktree of a registered repo, applies the LLM's patch files, and executes
swift_test or xcodebuild_test inside it. Returns combined output for
implementer retry feedback.
"""
from __future__ import annotations

import hmac
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import vm
from .config import Config
from .frameworks import (
    DEFAULT_TIMEOUTS,
    FrameworkError,
    SANDBOX_EXEC,
    VM_REQUIRED,
    build_cmd,
    build_resolve_cmd,
    wrap_sandbox,
)
from .environment import resolve_environment
from .integration import IntegrationError, check_patch_integrated
from .workspace import worktree, WorkspaceError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("mac_runner.server")

app = FastAPI(title="coding-model mac-runner", version="0.1.0")

# Cap on the unsandboxed package-resolution pre-step (DEV-294). Bounded
# separately from the test timeout: resolution can hit the network, and a hung
# fetch should not consume the whole budget the actual test run needs.
RESOLVE_TIMEOUT = int(os.getenv("CODING_MODEL_RUNNER_RESOLVE_TIMEOUT", "300"))


def _sandbox_available() -> bool:
    """Separate seam so tests can exercise both paths off-macOS."""
    return os.path.exists(SANDBOX_EXEC)


class PatchFile(BaseModel):
    path: str
    content: str


class RunTestsRequest(BaseModel):
    spec_id: str
    repo: str = Field(..., description="Symbolic name registered in repos.yml")
    base_ref: str = "HEAD"
    patch_files: list[PatchFile] = []
    framework: str
    timeout: Optional[int] = None
    # framework-specific options
    filter: Optional[str] = None
    scheme: Optional[str] = None
    destination: Optional[str] = None
    configuration: Optional[str] = None
    workspace: Optional[str] = None
    project: Optional[str] = None


class RunTestsResponse(BaseModel):
    passed: bool
    output: str
    duration_sec: float
    exit_code: Optional[int] = None
    # Files whose existing content the patch replaced, with before/after sizes
    # (DEV-492). The runner is the only component that ever sees both versions,
    # so it is the only one that can report this. Advisory: the orchestrator
    # decides what a shrink means, since only the spec knows whether a rewrite
    # was intended.
    overwrites: list[dict] = []
    integration_warnings: list[str] = []


class ReadFilesRequest(BaseModel):
    repo: str = Field(..., description="Symbolic name registered in repos.yml")
    base_ref: str = "HEAD"
    paths: list[str] = []


class ReadFileResult(BaseModel):
    path: str
    # Exactly one of these is set. `content is None` means the file could not
    # be read; `error` says why, so the caller can tell "does not exist at this
    # ref" (the design named a file that isn't there) apart from "too large".
    content: Optional[str] = None
    error: Optional[str] = None


class ReadFilesResponse(BaseModel):
    files: list[ReadFileResult] = []


# Caps exist to protect the *implementer's* token budget, not the runner's
# memory. A single-call implementer runs at 32k tokens (DEV-492's read path
# feeds straight into that prompt), so handing back a 400KB generated file
# would crowd out the design and the spec and make the fix less likely, not
# more. Better to report the file as over-cap and let the caller decide.
READ_FILES_PER_FILE_MAX_BYTES = int(
    os.getenv("CODING_MODEL_RUNNER_READ_FILE_MAX_BYTES", "262144"))
READ_FILES_TOTAL_MAX_BYTES = int(
    os.getenv("CODING_MODEL_RUNNER_READ_TOTAL_MAX_BYTES", "1048576"))
READ_FILES_MAX_PATHS = int(
    os.getenv("CODING_MODEL_RUNNER_READ_MAX_PATHS", "40"))


def _reject_unsafe_read_path(rel: str) -> Optional[str]:
    """Reason to refuse *rel*, or None if it is safe to read.

    `git show <ref>:<path>` is already confined to the repository tree — it
    resolves against the ref, not the filesystem, so it cannot reach /etc or
    follow a symlink out. These checks are defence in depth and, more usefully,
    they turn a confusing git error into a clear one.
    """
    if not rel or not rel.strip():
        return "empty path"
    if rel.startswith("/"):
        return "absolute paths are not accepted"
    if ".." in Path(rel).parts:
        return "path escapes the repository"
    return None


def _git_show(repo: Path, ref: str, rel: str) -> subprocess.CompletedProcess:
    """`git show ref:path` returning bytes, never raising on a missing path."""
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{rel}"],
        capture_output=True, check=False,
    )


async def verify_runner_key(x_runner_key: Optional[str] = Header(None)) -> None:
    if not Config.API_KEY:
        if Config.ALLOW_UNAUTH:
            return
        raise HTTPException(500, "runner misconfigured: CODING_MODEL_RUNNER_API_KEY is empty")
    if not x_runner_key or not hmac.compare_digest(x_runner_key, Config.API_KEY):
        raise HTTPException(401, "invalid or missing runner key")


@app.get("/health")
async def health() -> dict:
    """Unauthenticated liveness only.

    The repo list moved behind auth (DEV-170): it leaked the operator's
    project codenames to anyone who could reach :5050.
    """
    return {"status": "ok"}


@app.get("/v1/repos", dependencies=[Depends(verify_runner_key)])
async def list_repos() -> dict:
    """Registered repo names — authenticated (see health())."""
    return {"repos": sorted(Config.repos().keys())}


@app.post(
    "/v1/read_files",
    response_model=ReadFilesResponse,
    dependencies=[Depends(verify_runner_key)],
)
def read_files_endpoint(req: ReadFilesRequest) -> ReadFilesResponse:
    """Return file contents at *base_ref* so the implementer can edit rather
    than reinvent (DEV-492).

    Reads with `git show <ref>:<path>`, which resolves any path at any ref
    directly — no worktree, no checkout, no lifecycle to clean up. That matters
    because this runs on the dispatch path: a `worktree add`/`remove` pair per
    read would be far more expensive than the read itself.

    Per-file errors are returned in-band rather than raised. One unreadable
    path must not deny the implementer the other nine — a partial answer is
    strictly better than none, and the caller can see exactly what is missing.
    """
    repos = Config.repos()
    if req.repo not in repos:
        raise HTTPException(
            400,
            f"unknown repo '{req.repo}' — register it in {Config.REPOS_FILE}",
        )
    repo_path = repos[req.repo]

    results: list[ReadFileResult] = []
    budget = READ_FILES_TOTAL_MAX_BYTES
    for rel in req.paths[:READ_FILES_MAX_PATHS]:
        err = _reject_unsafe_read_path(rel)
        if err:
            results.append(ReadFileResult(path=rel, error=err))
            continue
        proc = _git_show(repo_path, req.base_ref, rel)
        if proc.returncode != 0:
            # git's own message names the ref and path; it is the most useful
            # thing to hand back ("path does not exist in <ref>"). stdout/stderr
            # are bytes here because the file itself may not be text.
            detail = proc.stderr.decode("utf-8", "replace").strip()
            results.append(ReadFileResult(
                path=rel, error=(detail or "git show failed")[:300],
            ))
            continue
        raw = proc.stdout
        if len(raw) > READ_FILES_PER_FILE_MAX_BYTES:
            results.append(ReadFileResult(
                path=rel,
                error=(f"file is {len(raw)} bytes, over the "
                       f"{READ_FILES_PER_FILE_MAX_BYTES}-byte per-file cap"),
            ))
            continue
        if len(raw) > budget:
            results.append(ReadFileResult(
                path=rel, error="total response budget exhausted"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            results.append(ReadFileResult(path=rel, error="not valid UTF-8"))
            continue
        budget -= len(raw)
        results.append(ReadFileResult(path=rel, content=text))

    found = sum(1 for r in results if r.content is not None)
    logger.info("read_files: repo=%s ref=%s requested=%d returned=%d",
                req.repo, req.base_ref, len(req.paths), found)
    return ReadFilesResponse(files=results)


@app.post(
    "/v1/run_tests",
    response_model=RunTestsResponse,
    dependencies=[Depends(verify_runner_key)],
)
def run_tests_endpoint(req: RunTestsRequest) -> RunTestsResponse:
    repos = Config.repos()
    if req.repo not in repos:
        raise HTTPException(
            400,
            f"unknown repo '{req.repo}' — register it in {Config.REPOS_FILE}",
        )
    repo_path = repos[req.repo]

    if req.framework not in DEFAULT_TIMEOUTS:
        raise HTTPException(400, f"unsupported framework: {req.framework}")

    timeout = req.timeout or DEFAULT_TIMEOUTS[req.framework]
    patch_dicts = [pf.model_dump() for pf in req.patch_files]
    # framework is passed positionally to build_cmd; leaving it in opts too
    # makes it a duplicate argument.
    opts = req.model_dump(exclude_none=True, exclude={"framework"})
    # VM containment (DEV-422): frameworks sandbox-exec cannot hold
    # (app-hosted XCTest, DEV-403) dispatch into a throwaway tart VM instead
    # of running unsandboxed on the host. CODING_MODEL_RUNNER_VM=0 is the
    # explicit fallback to the old warned host behavior.
    use_vm = Config.SANDBOX and Config.VM and req.framework in VM_REQUIRED

    # Discover what this Mac can offer: a real signing identity in preference
    # to ad-hoc, and an attached physical device in preference to a simulator
    # (DEV-395/DEV-396). Anything the plan set explicitly is left alone.
    # Host-only: an attached device is unreachable from inside a VM, and the
    # guest signs ad-hoc so there is no identity to discover (DEV-421).
    if req.framework == "xcodebuild_test" and not use_vm:
        opts = resolve_environment(opts)
        if device := opts.pop("destination_device", None):
            logger.info("testing on attached device %s (%s)",
                        device, opts.get("destination"))

    start = time.monotonic()
    if use_vm and (unavailable := vm.vm_available()):
        # An operator problem, not something the implementer can patch its
        # way out of (cf. the missing-toolchain path below) — and falling
        # back silently to an unconfined host run would defeat the point.
        logger.error("VM containment unavailable: %s", unavailable)
        return RunTestsResponse(
            passed=False,
            output=(f"[vm] VM containment is enabled for {req.framework} "
                    f"but unavailable: {unavailable}"),
            duration_sec=0.0,
            exit_code=None,
        )
    # DEV-492: the worktree still holds base_ref when the patch is written, so
    # this is the only place a "modify" can be compared against what it replaced.
    overwrites: list[dict] = []
    try:
        with worktree(
            repo_path, req.base_ref, req.spec_id,
            Config.WORKTREE_ROOT, patch_dicts, overwrites=overwrites,
        ) as wt:
            # A patch written where the project does not look is compiled by
            # nobody, and the repo's own green tests then report the run as a
            # PASS (DEV-399). Catch that here, before spending a build on it.
            try:
                # Warnings cover the PARTIAL case — some files landed, some did
                # not. They ride the run's output rather than a new response
                # field, because output already reaches the reviewer and the
                # gate, and DEV-492's `overwrites` is already a signal nothing
                # consumes (DEV-526).
                integration_warnings = check_patch_integrated(
                    wt, patch_dicts, req.framework)
            except IntegrationError as e:
                logger.error("spec %s: patch not integrated — %s", req.spec_id, e)
                return RunTestsResponse(
                    passed=False,
                    output=f"[integration check] {e}",
                    exit_code=None,
                    duration_sec=round(time.monotonic() - start, 2),
                    overwrites=overwrites,
                )

            # In VM mode the command runs in the guest, so it must name guest
            # paths — the guest resolves and builds into its own throwaway
            # DerivedData, never the host's.
            derived_data = (Path(vm.GUEST_DERIVED_DATA) if use_vm
                            else Config.DERIVED_DATA)
            try:
                cmd = build_cmd(req.framework, wt, derived_data, **opts)
                resolve_cmd = build_resolve_cmd(
                    req.framework, wt, derived_data, **opts)
            except FrameworkError as e:
                raise HTTPException(400, str(e))

            if use_vm:
                logger.info(
                    "running %s for spec %s in an ephemeral VM "
                    "(image %s, timeout=%ds)",
                    req.framework, req.spec_id, Config.VM_IMAGE, timeout)
                exit_code, output = vm.run_tests_in_vm(
                    wt, resolve_cmd, cmd,
                    timeout=timeout,
                    resolve_timeout=min(timeout, RESOLVE_TIMEOUT),
                )
                passed = exit_code == 0
            else:
                passed, output, exit_code = _run_on_host(
                    req, wt, cmd, resolve_cmd, timeout)
    except WorkspaceError as e:
        return RunTestsResponse(
            passed=False,
            output=f"workspace error: {e}",
            duration_sec=time.monotonic() - start,
            exit_code=None,
        )

    duration = time.monotonic() - start
    logger.info("test result: %s in %.1fs (%d chars output)",
                "PASS" if passed else "FAIL", duration, len(output))
    if integration_warnings:
        # First, not last: a 20k-char xcodebuild log buries a trailing note,
        # and this changes how the whole result should be read.
        output = "\n".join(integration_warnings) + "\n\n" + output
    for ow in overwrites:
        if ow.get("suspected_reconstruction"):
            logger.warning(
                "spec %s: %s was REPLACED, %d lines -> %d lines. A file the "
                "implementer never read is re-emitted from imagination "
                "(DEV-492); this shrink suggests reconstruction, not an edit.",
                req.spec_id, ow["path"], ow["old_lines"], ow["new_lines"])
    return RunTestsResponse(
        passed=passed, output=output.strip(),
        duration_sec=duration, exit_code=exit_code,
        overwrites=overwrites,
    )


def _run_on_host(req: RunTestsRequest, wt: Path, cmd: list[str],
                 resolve_cmd: "list[str] | None",
                 timeout: int) -> "tuple[bool, str, Optional[int]]":
    """The pre-DEV-422 host execution path: resolve, maybe sandbox, run."""
    # Resolve SwiftPM dependencies BEFORE sandboxing (DEV-294).
    # SwiftPM sandboxes manifest evaluation itself and macOS cannot
    # nest sandboxes, so doing this inside our profile fails with
    # "sandbox_apply: Operation not permitted" every time. Only the
    # build/test step — the part that runs the LLM-authored patch —
    # needs our confinement, and it still gets it below.
    resolve_output = ""
    if resolve_cmd is not None:
        logger.info("resolving packages (unsandboxed): %s",
                    " ".join(resolve_cmd))
        try:
            rr = subprocess.run(
                resolve_cmd, cwd=wt, capture_output=True, text=True,
                timeout=min(timeout, RESOLVE_TIMEOUT),
            )
            if rr.returncode != 0:
                # Not fatal on its own: the build may still succeed from
                # a warm cache, and if it cannot, xcodebuild's own error
                # is more useful than anything we would synthesise here.
                # Kept so a resolution failure is visible in the output
                # rather than showing up as a mystifying build error.
                logger.warning("package resolution exited %d", rr.returncode)
                resolve_output = (
                    "[package resolution failed — the build may fail "
                    f"for this reason]\n{rr.stdout}\n{rr.stderr}\n\n")
        except subprocess.TimeoutExpired:
            logger.warning("package resolution timed out")
            resolve_output = "[package resolution timed out]\n\n"
        except FileNotFoundError:
            logger.error("%s not found on PATH", resolve_cmd[0])
            resolve_output = f"[{resolve_cmd[0]!r} not found on PATH]\n\n"

    # The patch being built is LLM-authored code; confine it
    # (DEV-126). The Linux orchestrator already sandboxes its runs —
    # unsandboxed Mac execution was the asymmetry. The exemption list
    # is per-framework: app-hosted XCTest cannot survive sandbox-exec
    # at all (DEV-403) — its containment is the VM (DEV-422), so reaching
    # this path with such a framework means the operator disabled it.
    if Config.SANDBOX and req.framework in VM_REQUIRED:
        logger.warning(
            "%s runs UNSANDBOXED on the host: sandbox-exec cannot contain "
            "app-hosted XCTest (DEV-403) and VM containment (DEV-422) is "
            "disabled via CODING_MODEL_RUNNER_VM=0 — re-enable it to "
            "confine LLM-authored code",
            req.framework,
        )
    elif Config.SANDBOX and _sandbox_available():
        cmd = wrap_sandbox(
            cmd, profile=Config.SANDBOX_PROFILE, worktree=wt,
            derived_data=Config.DERIVED_DATA,
            signing_keychain=Config.SIGNING_KEYCHAIN,
        )
    elif Config.SANDBOX:
        logger.warning(
            "sandbox requested but %s not found — running "
            "LLM-authored tests UNSANDBOXED", SANDBOX_EXEC,
        )

    logger.info("running %s in %s (timeout=%ds)", " ".join(cmd), wt, timeout)
    try:
        result = subprocess.run(
            cmd, cwd=wt, capture_output=True, text=True, timeout=timeout,
        )
        passed = result.returncode == 0
        output = resolve_output + (result.stdout or "") + "\n" + (result.stderr or "")
        exit_code: Optional[int] = result.returncode
    except subprocess.TimeoutExpired as e:
        passed = False
        output = (
            f"Tests timed out after {timeout}s\n"
            f"{e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or '')}\n"
            f"{e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or '')}"
        )
        exit_code = None
    except FileNotFoundError:
        # Toolchain missing from PATH — an operator problem, not something
        # the implementer can patch its way out of. Log the PATH here; the
        # response only needs to name the binary.
        logger.error("%s not found on PATH=%s", cmd[0], os.environ.get("PATH", ""))
        passed = False
        output = (
            f"{cmd[0]!r} was not found on the runner's PATH. Install the "
            f"toolchain on the runner host, or fix PATH in the LaunchAgent plist."
        )
        exit_code = None
    return passed, output, exit_code


def unlock_signing_keychain() -> bool:
    """Unlock the signing keychain so headless codesign can reach the key.

    Keychains lock on every reboot. Without this, the first signed build
    after a restart fails at CodeSign with errSecInternalComponent — an error
    that names a missing certificate rather than the locked keychain
    (DEV-415/DEV-416). Failure is logged but not fatal: unsigned runs
    (CODE_SIGN_IDENTITY=-) don't need the keychain at all.
    """
    if not Config.SIGNING_KEYCHAIN:
        return True
    if not Config.SIGNING_KEYCHAIN_PASSWORD:
        logger.warning(
            "CODING_MODEL_RUNNER_SIGNING_KEYCHAIN is set but "
            "…_SIGNING_KEYCHAIN_PASSWORD is not — the keychain stays locked "
            "after a reboot and signed builds will fail with "
            "errSecInternalComponent until it is unlocked manually")
        return False
    proc = subprocess.run(
        ["security", "unlock-keychain",
         "-p", Config.SIGNING_KEYCHAIN_PASSWORD, Config.SIGNING_KEYCHAIN],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        logger.error("unlock of signing keychain %s failed: %s",
                     Config.SIGNING_KEYCHAIN, proc.stderr.strip())
        return False
    logger.info("signing keychain unlocked: %s", Config.SIGNING_KEYCHAIN)
    return True


def main() -> None:
    import uvicorn

    if not Config.API_KEY and not Config.ALLOW_UNAUTH:
        logger.error(
            "CODING_MODEL_RUNNER_API_KEY is not set. All test-run endpoints would be "
            "unauthenticated. Set it in ~/.config/coding-model-runner/.env, or set "
            "CODING_MODEL_RUNNER_ALLOW_UNAUTH=1 to explicitly permit (dev only)."
        )
        sys.exit(1)

    Config.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    Config.DERIVED_DATA.mkdir(parents=True, exist_ok=True)
    unlock_signing_keychain()

    logger.info(
        "mac_runner listening on %s:%d — %d registered repos",
        Config.HOST, Config.PORT, len(Config.repos()),
    )
    uvicorn.run(
        "mac_runner.server:app",
        host=Config.HOST, port=Config.PORT,
        log_level="info", loop="asyncio",
    )


if __name__ == "__main__":
    main()
