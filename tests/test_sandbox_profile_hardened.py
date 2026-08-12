"""The live mac_runner sandbox profile holds its boundary (DEV-527).

macOS-only: exercises the real /usr/bin/sandbox-exec against the deployed
profile (mac_runner/sandbox.sb) via mac_runner/validate_sandbox_profile.sh,
which is the executable form of the ticket's acceptance criteria — the
PATH-implant escape and IP egress are denied, credential stores stay
unreadable, system reads and a real toolchain compile still work. Skipped where
sandbox-exec does not exist (Linux CI), so it guards the profile on the runner
host where it matters. Points at the live sandbox.sb so it guards what actually
confines dispatched builds.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "mac_runner" / "validate_sandbox_profile.sh"
PROFILE = REPO / "mac_runner" / "sandbox.sb"


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
