"""Render a test run for a human gate — DEV-731.

The release gate is the last checkpoint before a spec is marked DONE and its
code is pushed. It embedded the test output as ``output[:3000]``.

xcodebuild output is front-loaded with boilerplate: the invocation, build
settings, resolved package versions, then a target dependency graph. On run 42
the first test result appeared at line 6,127 of a 1.3 MB log, so the gate prompt
ended mid-sentence inside the dependency tree and showed *no test result at all*.
The reviewer was asked to approve a push to a real repository on the strength of
"Resolve Package Graph".

Head truncation is not merely lossy here, it is anti-correlated with what
matters: the more dependencies a project has, the more certain it is that the
reviewer sees none of the results.

So: lead with the verdict and the roster, then show the END of the log, and say
plainly when anything was dropped and where the whole thing lives. A reviewer
who cannot see the tests is not reviewing.
"""
from __future__ import annotations

from . import diagnostics as _diagnostics

# The verdict and roster parser lives in diagnostics.py (DEV-838), beside the
# other readers of the same logs; these names stay importable from here.
_XCTEST_CASE = _diagnostics.XCTEST_CASE_RE
_SWIFT_TESTING = _diagnostics.SWIFT_TESTING_CASE_RE
_PYTEST_FAILED = _diagnostics.PYTEST_FAILED_RE
_PYTEST_SUMMARY = _diagnostics.PYTEST_PASSED_SUMMARY_RE
_VERDICTS = _diagnostics.VERDICT_BANNERS
_SWIFT_TESTING_RUN = _diagnostics.SWIFT_TESTING_RUN_VERDICT_RE
_XCTEST_ALL_SUITE = _diagnostics.XCTEST_ALL_SUITE_RE
_COMPILE_ERROR = _diagnostics.COMPILE_ERROR_LINE_RE
TestSummary = _diagnostics.TestSummary
summarize_test_output = _diagnostics.summarize_test_output


def _headline(s: TestSummary) -> str:
    if s.verdict and s.executed:
        counts = f"{s.executed} test(s) executed, {len(s.failed)} failed"
        return f"**{s.verdict}** — {counts}"
    if s.verdict:
        # A verdict with no roster is worth saying plainly: it is the shape a
        # build failure takes, and the shape of a VM that never ran the tests.
        return f"**{s.verdict}** — no individual test results in the output"
    if s.executed:
        return (f"**No framework verdict found** — {s.executed} test(s) "
                f"executed, {len(s.failed)} failed")
    return "**No test results found in the output**"


def render_for_gate(output: str, *, log_path: str | None = None,
                    budget: int = 3000, max_names: int = 40) -> str:
    """The test section of a gate prompt: verdict and roster first, then the
    TAIL of the log, with any omission stated.

    *budget* bounds the raw excerpt only; the summary above it is small and
    always shown, because it is the part being approved on.
    """
    text = output or ""
    s = summarize_test_output(text)
    lines = ["### Test result\n", _headline(s), ""]

    if s.failed:
        lines.append("**Failed:**")
        lines += [f"- `{n}`" for n in s.failed[:max_names]]
        if len(s.failed) > max_names:
            lines.append(f"- …and {len(s.failed) - max_names} more")
        lines.append("")
    if s.errors:
        lines.append("**Errors:**")
        lines += [f"- `{e}`" for e in s.errors[:max_names]]
        if len(s.errors) > max_names:
            lines.append(f"- …and {len(s.errors) - max_names} more")
        lines.append("")
    if s.n_passed:
        shown = s.passed[:max_names]
        rest = s.n_passed - len(shown)
        lines.append(
            f"**Passed ({s.n_passed}):** "
            + (", ".join(f"`{n}`" for n in shown) if shown else "")
            + ((" …and " if shown else "") + f"{rest} more" if rest else ""))
        lines.append("")

    excerpt = text[-budget:] if len(text) > budget else text
    if len(text) > budget:
        dropped_lines = text[:-budget].count("\n")
        where = f"; full log at `{log_path}`" if log_path else ""
        lines.append(
            f"_Showing the last {len(excerpt):,} of {len(text):,} characters — "
            f"{dropped_lines:,} earlier line(s) omitted{where}._")
    elif log_path:
        lines.append(f"_Full log at `{log_path}`._")
    lines.append(f"\n```\n{excerpt}\n```")
    return "\n".join(lines)
