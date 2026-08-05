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


# Frameworks that compile by DIRECTORY CONVENTION, where a file written one
# directory over is claimed by no target and silently compiled by nothing.
#
# swift_test was excluded until DEV-526 on the grounds that "a SwiftPM package
# compiles by directory convention, so Sources/… is correct there and this
# check would invert." Sources/ being correct does not make the check
# inapplicable: SwiftPM compiles only the directories a target CLAIMS, so
# Tests/CentipegeCoreTests/ (the DEV-502 typo) belongs to nothing, is not a
# tracked directory, and `landed()` flags it correctly. The guard was switched
# off on a false premise, and off for the framework the live pipeline uses.
DIRECTORY_CONVENTION_FRAMEWORKS = {"xcodebuild_test", "swift_test"}


def check_patch_integrated(worktree: Path, patch_files: list[dict],
                           framework: str) -> "list[str]":
    """Check that patched sources reach the build.

    Raises IntegrationError when NOTHING lands — deliberately conservative, per
    this module's design: an unfamiliar layout should stay silent rather than
    block a legitimate run.

    Returns warnings for the PARTIAL case, which used to be invisible: a patch
    of correct sources plus one mis-placed file passed without comment, because
    the check only ever asked "did anything land". That is DEV-502 — correct
    Sources/CentipedeCore/*.swift alongside a typo'd Tests/CentipegeCoreTests/,
    where the first exonerated the second and the suite went green having
    compiled none of the new tests.

    Warnings go back to the caller to be folded into the run's OUTPUT rather
    than into a new response field, because the orchestrator already forwards
    output to the reviewer and the gate. DEV-492's `overwrites` field is
    already a signal nothing consumes; a second one would help nobody.
    """
    if framework not in DIRECTORY_CONVENTION_FRAMEWORKS:
        return []

    sources = [Path(item["path"]) for item in patch_files
               if Path(item["path"]).suffix in SOURCE_SUFFIXES]
    if not sources:
        # Docs/config-only patches legitimately touch nothing compilable.
        return []

    tracked = _tracked_paths(worktree)
    if tracked is None:
        # git could not answer — an infrastructure fault, not a clean repo.
        # Reachable in practice: the runner is a LaunchAgent whose PATH comes
        # from its plist, so a missing git disables this guard on every run.
        # Previously one INFO line and a silent skip (DEV-526).
        logger.error(
            "integration check DISABLED for this run: git could not list "
            "tracked files in %s. A patch that reaches no build target will "
            "not be caught.", worktree)
        return ["[integration check] SKIPPED — git could not list tracked "
                "files, so this run has no protection against a patch that "
                "reaches no build target."]
    if not tracked:
        logger.info("repo has no tracked files — nothing to compare against")
        return []

    tracked_dirs = {str(Path(p).parent) for p in tracked}

    def landed(p: Path) -> bool:
        return p.as_posix() in tracked or str(p.parent) in tracked_dirs

    integrated = [p for p in sources if landed(p)]
    stranded = [p for p in sources if not landed(p)]

    if integrated:
        logger.info("patch integration: %d/%d source files land in the repo's "
                    "existing tree", len(integrated), len(sources))
        if not stranded:
            return []
        # The ratio was always computed and always discarded. Report it.
        listed = ", ".join(p.as_posix() for p in stranded[:8])
        near = sorted(d for d in tracked_dirs if d not in (".", ""))[:8]
        logger.warning(
            "spec patch: %d/%d source files land nowhere this repo keeps "
            "code: %s", len(stranded), len(sources), listed)
        return [
            f"[integration check] {len(stranded)} of {len(sources)} patched "
            f"source files land in no directory this repo uses, so nothing "
            f"compiles them and they cannot fail: {listed}. "
            f"Directories this repo does use: {', '.join(near)}. "
            f"The remaining {len(integrated)} did land, which is why the build "
            f"still ran — a green result here does NOT cover the files above."
        ]

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
