"""DEV-731: the gate must show the test result, not the build preamble."""
import pytest

from coding_model_autonomous.gate_output import (
    render_for_gate, summarize_test_output,
)

RUN42_TAIL = """\
Test suite 'BridgeLifecycleTests' started on 'My Mac - ElectricSheep (1724)'
Test case 'BridgeLifecycleTests.test_bridge_consume_after_prompt_reset()' passed on 'My Mac - ElectricSheep (1724)' (0.012 seconds)
Test case 'BridgeLifecycleTests.test_exact_once_consumption()' passed on 'My Mac - ElectricSheep (1724)' (0.105 seconds)
Test case 'BridgeLifecycleTests.test_same_instance_reset_consumes_from_start()' passed on 'My Mac - ElectricSheep (1724)' (0.025 seconds)
Test case 'BridgeLifecycleTests.test_window_cap_and_cursor_advance()' passed on 'My Mac - ElectricSheep (1724)' (0.013 seconds)
Test case 'SimulatorTests/clearParticles()' passed on 'My Mac - ElectricSheep (1718)' (1.215 seconds)
** TEST SUCCEEDED **
"""

# The shape that defeated the old gate: thousands of lines of xcodebuild
# boilerplate, then the results. Run 42's first result was at line 6,127.
RUN42_PREAMBLE = (
    "Command line invocation:\n    /Applications/Xcode_26.5.app/.../xcodebuild test\n"
    + "Resolve Package Graph\n"
    + "".join(f"    Target 'Dep{i}' in project 'mlx-swift'\n"
             f"        ➜ Explicit dependency on target 'Dep{i-1}'\n"
             for i in range(3000))
)
RUN42_FULL = RUN42_PREAMBLE + RUN42_TAIL


class TestRun42Regression:
    """The exact failure: a 1.3 MB log whose results are past any head limit."""

    def test_the_old_head_truncation_would_have_shown_nothing(self):
        """Establishes the bug is real before asserting the fix (negative control)."""
        old = RUN42_FULL[:3000]
        assert "TEST SUCCEEDED" not in old
        assert "test_exact_once_consumption" not in old
        assert "Resolve Package Graph" in old      # this is what it DID show

    def test_the_gate_now_leads_with_the_verdict(self):
        out = render_for_gate(RUN42_FULL)
        assert "**TEST SUCCEEDED**" in out
        assert "5 test(s) executed, 0 failed" in out

    def test_every_new_test_name_is_visible(self):
        out = render_for_gate(RUN42_FULL)
        for name in ("test_exact_once_consumption",
                     "test_window_cap_and_cursor_advance",
                     "test_same_instance_reset_consumes_from_start",
                     "test_bridge_consume_after_prompt_reset"):
            assert name in out, name

    def test_the_omission_is_stated_and_the_log_named(self):
        out = render_for_gate(RUN42_FULL, log_path="var/.../test_output.txt")
        assert "earlier line(s) omitted" in out
        assert "var/.../test_output.txt" in out

    def test_the_excerpt_is_the_tail(self):
        out = render_for_gate(RUN42_FULL)
        assert "** TEST SUCCEEDED **" in out.split("```")[-2]


class TestFailures:
    def test_a_failing_run_names_the_failing_cases_not_the_preamble(self):
        log = RUN42_PREAMBLE + (
            "Test case 'BridgeLifecycleTests.test_exact_once_consumption()' "
            "failed on 'My Mac' (0.10 seconds)\n"
            "Test case 'SimulatorTests/clearParticles()' passed on 'My Mac' (1.2 seconds)\n"
            "** TEST FAILED **\n")
        out = render_for_gate(log)
        assert "**TEST FAILED**" in out
        assert "2 test(s) executed, 1 failed" in out
        assert "**Failed:**" in out
        assert "test_exact_once_consumption" in out

    def test_compile_errors_are_surfaced(self):
        log = ("/Users/admin/work/ElectricSheepTests/BridgeLifecycleTests.swift:52:19: "
               "error: call to main actor-isolated initializer\n** BUILD FAILED **\n")
        out = render_for_gate(log)
        assert "**BUILD FAILED**" in out
        assert "**Errors:**" in out
        assert "main actor-isolated initializer" in out

    def test_a_verdict_with_no_roster_says_so(self):
        """A VM that never ran the tests looks exactly like this, and the gate
        must not imply tests were executed."""
        out = render_for_gate("** TEST FAILED **\n")
        assert "no individual test results" in out


class TestOtherFrameworks:
    def test_swift_testing_style(self):
        log = ('✔ Test "cursor advances" passed after 0.1 seconds.\n'
               '✘ Test "cursor resets" failed after 0.2 seconds.\n')
        s = summarize_test_output(log)
        assert s.passed == ["cursor advances"] and s.failed == ["cursor resets"]

    def test_pytest_style(self):
        log = ("FAILED tests/test_a.py::test_one\n"
               "=========== 1 failed, 41 passed in 2.20s ===========\n")
        out = render_for_gate(log)
        assert "tests/test_a.py::test_one" in out
        assert "42 test(s) executed, 1 failed" in out


class TestNoRegression:
    def test_short_output_is_shown_whole_with_no_omission_notice(self):
        out = render_for_gate(RUN42_TAIL)
        assert "omitted" not in out
        assert "Test suite 'BridgeLifecycleTests' started" in out

    def test_empty_output_does_not_claim_a_result(self):
        out = render_for_gate("")
        assert "**No test results found in the output**" in out

    @pytest.mark.parametrize("junk", ["", None, "   \n\n"])
    def test_junk_is_survivable(self, junk):
        assert isinstance(render_for_gate(junk), str)


# ── DEV-774: `swift test` verdicts (Swift Testing run lines, XCTest 'All tests')

RUN49_SWIFT_TEST = """Build complete! (12.3s)
Test Suite 'All tests' started at 2026-09-20 11:39:01.100.
Test Case '-[CentipedeCoreTests.GameTests testShotDestroysSpider]' passed (0.001 seconds).
Test Suite 'All tests' passed at 2026-09-20 11:39:01.400.
\t Executed 20 tests, with 0 failures (0 unexpected) in 0.3 (0.3) seconds
◇ Test run started.
✔ Test "criterion2_emptyFrame" passed after 0.012 seconds.
✔ Test "criterion3_oneMushroomExtent" passed after 0.010 seconds.
✔ Suite OffscreenRendererTests passed after 0.064 seconds.
✔ Test run with 5 tests in 1 suite passed after 0.064 seconds.
✔ Test run with 20 tests in 0 suites passed after 0.001 seconds.
"""


def test_swift_test_output_yields_a_pass_verdict_dev774():
    from coding_model_autonomous.gate_output import render_for_gate, summarize_test_output
    s = summarize_test_output(RUN49_SWIFT_TEST)
    assert s.verdict == "TEST RUN PASSED"
    out = render_for_gate(RUN49_SWIFT_TEST)
    assert out.startswith("### Test result\n\n**TEST RUN PASSED**")
    assert "No framework verdict found" not in out


def test_one_failed_swift_testing_runner_fails_the_verdict_dev774():
    from coding_model_autonomous.gate_output import summarize_test_output
    failed = RUN49_SWIFT_TEST.replace(
        "✔ Test run with 5 tests in 1 suite passed after 0.064 seconds.",
        "✘ Test run with 5 tests in 1 suite failed after 0.064 seconds with 1 issue.")
    assert summarize_test_output(failed).verdict == "TEST RUN FAILED"
    xc_failed = RUN49_SWIFT_TEST.replace("'All tests' passed at", "'All tests' failed at")
    assert summarize_test_output(xc_failed).verdict == "TEST RUN FAILED"


def test_xcodebuild_and_pytest_verdicts_unchanged_dev774():
    from coding_model_autonomous.gate_output import summarize_test_output
    assert summarize_test_output("** TEST SUCCEEDED **\n" + RUN49_SWIFT_TEST).verdict == "TEST SUCCEEDED"
    assert summarize_test_output("=== 3 passed in 0.1s ===\n").verdict is None
