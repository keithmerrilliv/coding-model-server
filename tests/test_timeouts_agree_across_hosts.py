"""DEV-705: the two DEFAULT_TIMEOUTS tables must agree, or a raise is inert.

The orchestrator (this repo, running on zooshly) and the mac runner
(mac_runner/, running on the MacBook Pro) each carry a per-framework timeout
table. A dispatch is bounded by BOTH: the caller abandons the request while the
runner is still working, so the effective budget is the minimum.

DEV-705 raised xcodebuild_test to 1200s in mac_runner/frameworks.py only. The
caller stayed at 900s, so the budget stayed at 900s and the raise did nothing —
caught on run 42, which dispatched at `timeout=900s` a day after the ticket was
recorded as fixed. A fix that reads as shipped and is not is worse than an open
ticket, so the agreement is pinned here rather than left to reviewers.

These are two hosts and one repo (see the two-host deploy rule): the halves are
deployed by different actions, which is exactly why nothing else notices when
they drift.
"""
import importlib.util
import sys
from pathlib import Path

from coding_model_autonomous.test_runner import DEFAULT_TIMEOUTS as CALLER


def _runner_timeouts() -> dict:
    """mac_runner/frameworks.py's table, loaded by path.

    mac_runner is not an installed package here — it is deployed to the Mac —
    so this loads the module directly. It imports only the standard library,
    which is what makes that safe to do from a Linux test run.
    """
    path = Path(__file__).resolve().parents[1] / "mac_runner" / "frameworks.py"
    spec = importlib.util.spec_from_file_location("_mac_runner_frameworks", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DEFAULT_TIMEOUTS


def test_timeouts_agree_across_hosts():
    runner = _runner_timeouts()
    shared = set(CALLER) & set(runner)
    assert shared, "no shared frameworks — the tables cannot have drifted, check the import"
    mismatched = {k: (CALLER[k], runner[k]) for k in sorted(shared) if CALLER[k] != runner[k]}
    assert not mismatched, (
        "timeout tables disagree (caller, runner); the effective budget is the "
        f"minimum, so the larger value is inert: {mismatched}"
    )


def test_xcodebuild_carries_the_dev705_budget():
    """The specific value the ticket is about, pinned on both sides.

    DEV-705 set 1200 and DEV-752 deliberately left it there: run 44's failure
    was a starved host, not a slow resolve, and a bigger ceiling only lengthens
    the wait before a starved dispatch reports.
    """
    assert CALLER["xcodebuild_test"] == 1200
    assert _runner_timeouts()["xcodebuild_test"] == 1200
