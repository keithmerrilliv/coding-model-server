"""Per-framework command builders for the mac runner."""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Per-framework default timeouts (seconds). Cold xcodebuild can be slow;
# swift test is faster but still heavier than pytest.
DEFAULT_TIMEOUTS: dict[str, int] = {
    "swift_test": 300,
    "xcodebuild_test": 900,
}


class FrameworkError(ValueError):
    pass


def build_swift_test_cmd(worktree: Path, **opts: Any) -> list[str]:
    cmd = ["swift", "test", "--parallel"]
    if filt := opts.get("filter"):
        cmd.extend(["--filter", filt])
    return cmd


def build_xcodebuild_test_cmd(worktree: Path, derived_data: Path, **opts: Any) -> list[str]:
    scheme = opts.get("scheme")
    if not scheme:
        raise FrameworkError("xcodebuild_test requires 'scheme'")
    destination = opts.get("destination", "platform=macOS")
    configuration = opts.get("configuration", "Debug")
    cmd = [
        "xcodebuild", "test",
        "-scheme", scheme,
        "-destination", destination,
        "-configuration", configuration,
        "-derivedDataPath", str(derived_data),
        # LLM-generated test targets should not require signed binaries.
        "CODE_SIGNING_ALLOWED=NO",
        "CODE_SIGNING_REQUIRED=NO",
        "CODE_SIGN_IDENTITY=",
    ]
    if ws := opts.get("workspace"):
        cmd.extend(["-workspace", ws])
    elif project := opts.get("project"):
        cmd.extend(["-project", project])
    # If neither specified, xcodebuild auto-detects a .xcodeproj/.xcworkspace in cwd.
    return cmd


def build_cmd(framework: str, worktree: Path, derived_data: Path, **opts: Any) -> list[str]:
    if framework == "swift_test":
        return build_swift_test_cmd(worktree, **opts)
    if framework == "xcodebuild_test":
        return build_xcodebuild_test_cmd(worktree, derived_data, **opts)
    raise FrameworkError(f"unsupported framework: {framework}")
