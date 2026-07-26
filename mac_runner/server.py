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
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Config
from .frameworks import (
    DEFAULT_TIMEOUTS,
    FrameworkError,
    SANDBOX_EXEC,
    build_cmd,
    build_resolve_cmd,
    wrap_sandbox,
)
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

    start = time.monotonic()
    try:
        with worktree(
            repo_path, req.base_ref, req.spec_id,
            Config.WORKTREE_ROOT, patch_dicts,
        ) as wt:
            try:
                cmd = build_cmd(req.framework, wt, Config.DERIVED_DATA, **opts)
            except FrameworkError as e:
                raise HTTPException(400, str(e))

            # Resolve SwiftPM dependencies BEFORE sandboxing (DEV-294).
            # SwiftPM sandboxes manifest evaluation itself and macOS cannot
            # nest sandboxes, so doing this inside our profile fails with
            # "sandbox_apply: Operation not permitted" every time. Only the
            # build/test step — the part that runs the LLM-authored patch —
            # needs our confinement, and it still gets it below.
            resolve_output = ""
            try:
                resolve_cmd = build_resolve_cmd(
                    req.framework, wt, Config.DERIVED_DATA, **opts)
            except FrameworkError as e:
                raise HTTPException(400, str(e))
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
            # unsandboxed Mac execution was the asymmetry.
            if Config.SANDBOX and _sandbox_available():
                cmd = wrap_sandbox(
                    cmd, profile=Config.SANDBOX_PROFILE, worktree=wt,
                    derived_data=Config.DERIVED_DATA,
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
    return RunTestsResponse(
        passed=passed, output=output.strip(),
        duration_sec=duration, exit_code=exit_code,
    )


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
