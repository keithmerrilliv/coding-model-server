"""DEV-713: a known-flaky test must be excludable, or it charges the model.

DEV-603 measured DtypeContainmentTests a1/a2 failing on roughly half of
Electric Sheep runs with no relation to the code under test — byte-identical
patch content, minutes apart, opposite results — and sometimes aborting the
suite so two thirds of it never ran. The runner had only `-only-testing:`,
so there was no way to stop that coin flip being charged to the implementer.
"""

import pytest

from mac_runner.frameworks import build_swift_test_cmd, build_xcodebuild_test_cmd


def _xcode(**opts):
    return build_xcodebuild_test_cmd(
        __import__("pathlib").Path("/tmp/wt"),
        __import__("pathlib").Path("/tmp/dd"),
        scheme="ElectricSheep", **opts)


def test_xcodebuild_skips_the_quarantined_class():
    cmd = _xcode(filter="ElectricSheepTests",
                 skip_filter="ElectricSheepTests/DtypeContainmentTests")
    assert "-skip-testing:ElectricSheepTests/DtypeContainmentTests" in cmd
    # the rest of the target still runs
    assert "-only-testing:ElectricSheepTests" in cmd


def test_xcodebuild_accepts_a_list_of_exclusions():
    cmd = _xcode(skip_filter="ES/A, ES/B ES/C")
    assert [c for c in cmd if c.startswith("-skip-testing:")] == [
        "-skip-testing:ES/A", "-skip-testing:ES/B", "-skip-testing:ES/C"]


def test_xcodebuild_without_a_quarantine_excludes_nothing():
    """Negative control — the common case must be unchanged."""
    cmd = _xcode(filter="ElectricSheepTests")
    assert not any(c.startswith("-skip-testing:") for c in cmd)


def test_swift_test_skips_the_quarantined_class():
    cmd = build_swift_test_cmd(__import__("pathlib").Path("/tmp/wt"),
                               filter="CoreTests", skip_filter="CoreTests/Flaky")
    assert cmd[cmd.index("--skip") + 1] == "CoreTests/Flaky"


def test_swift_test_without_a_quarantine_excludes_nothing():
    """Negative control."""
    cmd = build_swift_test_cmd(__import__("pathlib").Path("/tmp/wt"),
                               filter="CoreTests")
    assert "--skip" not in cmd


@pytest.mark.parametrize("raw", ["", None])
def test_an_empty_quarantine_is_not_an_exclusion(raw):
    """An empty string must not become `-skip-testing:` with no selector."""
    assert not any(c.startswith("-skip-testing:") for c in _xcode(skip_filter=raw))


def test_the_planner_cannot_drop_an_operator_quarantine():
    """A quarantine the operator declared is enforced, not suggested (DEV-573)."""
    import yaml
    from coding_model_server.orchestrator_daemon import (
        _overlay_operator_test_strategy)
    spec = """# Spec

## Test strategy

- framework: xcodebuild_test
- repo: electric-sheep
- filter: ElectricSheepTests
- skip_filter: ElectricSheepTests/DtypeContainmentTests
"""
    plan = ("title: t\ntest_strategy:\n  framework: xcodebuild_test\n"
            "  repo: electric-sheep\n  filter: ElectricSheepTests\n")
    out = _overlay_operator_test_strategy(plan, spec, "spec_test")
    strategy = yaml.safe_load(out)["test_strategy"]
    assert strategy["skip_filter"] == "ElectricSheepTests/DtypeContainmentTests"
