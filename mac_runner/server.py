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
from .frameworks import DEFAULT_TIMEOUTS, build_cmd, FrameworkError
from .workspace import worktree, WorkspaceError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("mac_runner.server")

app = FastAPI(title="coding-model mac-runner", version="0.1.0")


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
    return {"status": "ok", "repos": sorted(Config.repos().keys())}


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

            logger.info("running %s in %s (timeout=%ds)", " ".join(cmd), wt, timeout)
            try:
                result = subprocess.run(
                    cmd, cwd=wt, capture_output=True, text=True, timeout=timeout,
                )
                passed = result.returncode == 0
                output = (result.stdout or "") + "\n" + (result.stderr or "")
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
