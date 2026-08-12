"""The hardened mac_runner sandbox profile holds its boundary (DEV-527).

macOS-only: exercises the real /usr/bin/sandbox-exec against the hardened
candidate profile via mac_runner/validate_sandbox_profile.sh, which is the
executable form of the ticket's acceptance criteria — the PATH-implant escape
and IP egress are denied, credential stores stay unreadable, system reads and a
real toolchain compile still work. Skipped where sandbox-exec does not exist
(Linux CI), so it guards the profile on the runner host where it matters.

When the candidate is promoted (sandbox.hardened.sb -> sandbox.sb), point
PROFILE at the live file so this keeps guarding what actually ships.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "mac_runner" / "validate_sandbox_profile.sh"
PROFILE = REPO / "mac_runner" / "sandbox.hardened.sb"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(),
                    reason="sandbox-exec not present")
def test_hardened_profile_holds_the_boundary():
    result = subprocess.run(
        ["bash", str(SCRIPT), str(PROFILE)],
        capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, (
        "sandbox profile validation failed:\n" + result.stdout + result.stderr)
