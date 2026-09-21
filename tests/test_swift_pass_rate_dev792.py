"""DEV-792: Swift runner summaries yield a pass rate, so a near-miss synthesis gets its repair round."""
import pytest
import coding_model_server.orchestrator_daemon as d

SWIFT_TESTING = """\
\x1b[32m✔\x1b[0m Test run with 7 tests in 2 suites passed after 0.065 seconds.
✘ Test precedenceSpiderOverMushroom() recorded an issue at BoardSnapshotTests.swift:109:9: Expectation failed: sm.entities[BoardCell(column: 10, row: 27)] == BoardEntity.spider
✘ Test precedenceSpiderOverMushroom() failed after 0.001 seconds with 1 issue.
✘ Suite BoardSnapshotTests failed after 0.001 seconds with 1 issue.
✘ Test run with 24 tests in 1 suite failed after 0.001 seconds with 1 issue.
"""

XCODEBUILD = """\
Test case 'AudioLifecycleTests.test_interruption_stops_playback()' passed on 'My Mac - ElectricSheep (1720)' (0.024 seconds)
Test case 'ProductionDtypeConversionTests/unsupportedDtypeStillContained()' failed on 'My Mac - ElectricSheep (1713)' (1.595 seconds)
Test case 'GPULayoutTests/size()' passed on 'My Mac - ElectricSheep (1713)' (1.593 seconds)
Test case 'ProductionDtypeConversionTests/productionDtypesDoNotReport()' failed on 'My Mac - ElectricSheep (1713)' (1.901 seconds)
** TEST FAILED **
"""

XCTEST = "Test Suite 'All tests' failed at 2026-09-21.\n\t Executed 12 tests, with 2 failures (0 unexpected) in 0.5 seconds\n"


def test_swift_testing_run_52_is_thirty_of_thirty_one():
    assert d._test_pass_rate(SWIFT_TESTING) == pytest.approx(30 / 31)


def test_xcodebuild_per_case_lines_count():
    assert d._test_pass_rate(XCODEBUILD) == pytest.approx(2 / 4)


def test_xctest_executed_summary():
    assert d._test_pass_rate(XCTEST) == pytest.approx(10 / 12)


def test_non_swift_shapes_unchanged():
    assert d._test_pass_rate("15 passed, 2 failed in 3.2s") == pytest.approx(15 / 17)
    assert d._test_pass_rate("no summary here") is None


def test_actionable_swift_output_leads_with_the_failure():
    out = d._extract_actionable_test_output(SWIFT_TESTING, "swift_test")
    assert out.splitlines()[0].startswith("✘ Test precedenceSpiderOverMushroom() recorded an issue")
    assert "Expectation failed" in out and "Test run with 24 tests" in out
    assert "Suite BoardSnapshotTests" not in out
