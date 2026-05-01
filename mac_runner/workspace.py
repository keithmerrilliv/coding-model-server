"""Git-worktree-per-spec lifecycle for the mac runner."""
from __future__ import annotations

import contextlib
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("mac_runner.workspace")


class WorkspaceError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _write_patch_files(worktree_path: Path, patch_files: list[dict]) -> None:
    worktree_abs = worktree_path.resolve()
    for item in patch_files:
        rel = item["path"]
        content = item["content"]
        # resolve() follows symlinks, so a symlink inside the worktree that
        # points outside of it will be caught by the prefix check below.
        dest = (worktree_path / rel).resolve()
        if dest != worktree_abs and not str(dest).startswith(str(worktree_abs) + "/"):
            raise WorkspaceError(f"patch path escapes worktree: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


@contextlib.contextmanager
def worktree(
    repo: Path,
    base_ref: str,
    spec_id: str,
    root: Path,
    patch_files: list[dict],
) -> Iterator[Path]:
    """Create a git worktree, apply patch files, yield its path, clean up on exit.

    --force is used on removal to discard any uncommitted files left by the
    test run (e.g. xcodebuild caches, .pytest_cache, generated fixtures).
    """
    git_marker = repo / ".git"
    if not git_marker.exists():
        raise WorkspaceError(f"not a git repo: {repo}")
    root.mkdir(parents=True, exist_ok=True)

    # unique sub-path so concurrent runs on the same spec don't collide
    unique = f"{spec_id}-{uuid.uuid4().hex[:8]}"
    path = root / unique
    logger.info("creating worktree %s from %s@%s", path, repo, base_ref)
    try:
        _git(repo, "worktree", "add", "--detach", str(path), base_ref)
    except subprocess.CalledProcessError as e:
        raise WorkspaceError(f"git worktree add failed: {e.stderr.strip()}") from e

    try:
        _write_patch_files(path, patch_files)
        yield path
    finally:
        logger.info("removing worktree %s", path)
        _git(repo, "worktree", "remove", "--force", str(path), check=False)
        # defensive rm -rf in case `worktree remove` couldn't clean up (e.g.
        # file permission weirdness from xcodebuild)
        if path.exists():
            subprocess.run(["rm", "-rf", str(path)], check=False)
