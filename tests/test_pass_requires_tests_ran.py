"""DEV-513 — PASS requires evidence that tests actually ran.

`tests_passed` used to be initialised True and left untouched whenever the
suite was skipped:

    tests_passed = True
    if tests_required and result.test_files:
        tests_passed, test_output = _run_reviewer_tests(...)
    ...
    if tests_passed and result.verdict == "PASS":
        "Tests **PASSED**. Reviewer verdict: **PASS**."

So a reviewer that returned a well-formed PASS verdict with ZERO <<<FILE:>>>
blocks reached the release gate reporting "Tests PASSED" having executed
nothing. `tests_required` gated whether the suite RAN, never whether the
verdict was allowed to be PASS — it was unenforceable.

This is one of four entry points into the same defect class found by the
2026-08-04 audit, and the same class as DEV-502, where a test file written one
character off its target directory compiled as nothing and the suite went
green. The unifying rule: absence of evidence is not a pass.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "src/coding_model_server/orchestrator_daemon.py")


def _reviewer_gate_source() -> str:
    """The reviewer-completion block, from the test-run setup to the gate."""
    body = SRC.read_text()
    start = body.index("# Run tests if required")
    end = body.index("# ── Supervisor-driven transition layer", start)
    return body[start:end]


class TestTheInvariant:
    def test_tests_passed_does_not_start_true(self):
        """The whole defect in one line."""
        src = _reviewer_gate_source()
        assert "tests_passed = False" in src
        # The only place it may be set True unconditionally is the explicit
        # "the plan waived tests" branch; a bare `tests_passed = True` sitting
        # above the run would reintroduce the bug.
        first_assign = src.index("tests_passed = ")
        assert src[first_assign:first_assign + 22].strip() == "tests_passed = False", (
            "tests_passed must start False — a True default survives an unrun "
            "suite and reports PASS at the release gate")

    def test_a_workspace_with_no_tests_is_not_a_pass(self):
        """A suite that does not exist must not reach the gate green.

        Originally this asserted on `not result.test_files` — whether the
        REVIEWER emitted tests. That is a different proposition from whether a
        suite exists, and it fired twice in production against runs where the
        implementer's tests had already run and passed, discarding a
        verified-green result each time. The invariant is unchanged; the
        question it asks is now the right one.
        """
        src = _reviewer_gate_source()
        assert "_workspace_has_test_files(" in src, (
            "the no-tests-anywhere case must be handled explicitly, not fall "
            "through to the initial value")

    def test_the_no_tests_check_considers_both_roles(self):
        """Implementer-written tests are just as real as reviewer-written ones."""
        from coding_model_server.orchestrator_daemon import _workspace_has_test_files
        assert _workspace_has_test_files([("a/bTests.swift", "")], []) is True
        assert _workspace_has_test_files([], [("tests/test_a.py", "")]) is True
        assert _workspace_has_test_files([("a/b.swift", "")], []) is False

    def test_a_waived_suite_is_the_only_unconditional_pass(self):
        """tests_required=False is a deliberate operator choice and may pass."""
        src = _reviewer_gate_source()
        assert "not tests_required" in src

    def test_the_failure_says_why_no_tests_ran(self):
        """Otherwise the report carries an empty output fence, which reads as
        'the tests ran and printed nothing' and sends the implementer hunting
        a phantom failure."""
        src = _reviewer_gate_source()
        assert "No tests were executed" in src
        assert "tests_skipped_reason" in src

    def test_the_gate_still_claims_passed_only_on_the_pass_path(self):
        """The 'Tests **PASSED**' string must remain reachable only when
        tests_passed is genuinely True."""
        src = _reviewer_gate_source()
        # The f-string form, not the bare words: the bare words also appear in
        # the comment explaining this defect, and matching those would make
        # this assertion pass on prose rather than on code.
        claim = src.index('f"Tests **PASSED**')
        pass_branch = src.index('if tests_passed and result.verdict == "PASS"')
        assert claim > pass_branch, "the claim must sit inside the PASS branch"


class TestRegressionShape:
    def test_the_old_default_is_gone(self):
        """Guard the exact line that caused it, so a refactor cannot restore it."""
        src = _reviewer_gate_source()
        bad = re.search(r"tests_passed\s*=\s*True\s*\n\s*test_output\s*=\s*\"\"",
                        src)
        assert bad is None, (
            "the True/empty-string pair above the conditional run is the "
            "original DEV-513 defect")
