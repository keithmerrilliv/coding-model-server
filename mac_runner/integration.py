"""Verify that a patch actually reached the build (DEV-399).

A patch written to a path the project does not reference is silently ignored:
xcodebuild compiles nothing new, the repo's own tests still pass, and the run is
reported PASS. That happened on spec_7ebcdb2e — the implementer emitted
SwiftPM-style `Sources/…` and `Tests/…` for a plain Xcode project whose sources
live in `ElectricSheep/` and `ElectricSheepTests/`, so none of its work was
compiled and the spec reached DONE having changed nothing. The greener a repo's
baseline, the more convincing that false pass looks.

Approach: ask git what the repo actually contains. A patched source file is
plausibly integrated if it edits a tracked file, or lands in a directory that
already holds tracked files. A file dropped into a directory tree the repo does
not have is the signature of a guessed layout.

Basename matching was tried first and is wrong: `Sources/Foo.swift` and
`ElectricSheep/Foo.swift` share a basename, so the check passed the very case it
existed to catch. Paths matter, not names.

The check is deliberately conservative — it fails only when confident that
nothing landed, so an unfamiliar layout stays silent rather than blocking a
legitimate run.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("mac_runner.integration")

SOURCE_SUFFIXES = {".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h", ".hpp"}


class IntegrationError(Exception):
    """A patch was applied but nothing it wrote is part of the build."""


def _tracked_paths(worktree: Path) -> "set[str] | None":
    """Repo-relative paths git tracks here, or None if that cannot be determined.

    Tracked content is the repo as it was before the patch: `_write_patch_files`
    only ever adds untracked files, so this is a clean picture of the real
    layout even though it runs after the patch has been written.
    """
    try:
        r = subprocess.run(["git", "ls-files"], cwd=worktree,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("git ls-files failed in %s: %s", worktree, e)
        return None
    if r.returncode != 0:
        logger.warning("git ls-files exited %d in %s", r.returncode, worktree)
        return None
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def check_patch_integrated(worktree: Path, patch_files: list[dict],
                           framework: str) -> None:
    """Raise IntegrationError when no patched source file reaches the build.

    Only applied to xcodebuild runs. A SwiftPM package compiles by directory
    convention, so `Sources/…` is correct there and this check would invert.
    """
    if framework != "xcodebuild_test":
        return

    sources = [Path(item["path"]) for item in patch_files
               if Path(item["path"]).suffix in SOURCE_SUFFIXES]
    if not sources:
        # Docs/config-only patches legitimately touch nothing compilable.
        return

    tracked = _tracked_paths(worktree)
    if not tracked:
        logger.info("no tracked files resolved — skipping integration check")
        return

    tracked_dirs = {str(Path(p).parent) for p in tracked}

    def landed(p: Path) -> bool:
        return p.as_posix() in tracked or str(p.parent) in tracked_dirs

    integrated = [p for p in sources if landed(p)]
    if integrated:
        logger.info("patch integration OK: %d/%d source files land in the repo's "
                    "existing tree", len(integrated), len(sources))
        return

    listed = ", ".join(p.as_posix() for p in sources[:6])
    example = sorted(d for d in tracked_dirs if d not in (".", ""))[:6]
    raise IntegrationError(
        "no patched source file lands anywhere this repo actually keeps code, so "
        "xcodebuild would compile none of them and the run would pass on the "
        "strength of the repo's existing tests alone (DEV-399). "
        f"Patched: {listed}. "
        f"Directories this repo does use: {', '.join(example)}. "
        "The implementer most likely guessed the project layout — write to the "
        "paths this project really uses."
    )
