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

import re
from dataclasses import dataclass, field

# XCTest, as xcodebuild prints it. Note the lowercase "case" — the capitalised
# form does not appear, and grepping for "Test Case" finds nothing, which is a
# good way to conclude no tests ran when four of them did.
_XCTEST_CASE = re.compile(
    r"^Test case '([^']+)'\s+(passed|failed)\b", re.MULTILINE)
# swift-testing (@Test / @Suite), Xcode 16+.
_SWIFT_TESTING = re.compile(
    r'^[✔✘◇\s]*Test\s+(?:"([^"]+)"|(\S+?))\s+(passed|failed)\b', re.MULTILINE)
# pytest per-failure lines and its summary line.
_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_PYTEST_SUMMARY = re.compile(
    r"^=+\s*(?:.*?\b(\d+) failed)?.*?\b(\d+) passed\b.*$", re.MULTILINE)
# The framework-level verdicts.
_VERDICTS = (
    (re.compile(r"\*\*\s*TEST SUCCEEDED\s*\*\*"), "TEST SUCCEEDED"),
    (re.compile(r"\*\*\s*TEST FAILED\s*\*\*"), "TEST FAILED"),
    (re.compile(r"\*\*\s*BUILD SUCCEEDED\s*\*\*"), "BUILD SUCCEEDED"),
    (re.compile(r"\*\*\s*BUILD FAILED\s*\*\*"), "BUILD FAILED"),
)
# Compiler/link errors, so a build failure names its cause rather than making
# the reviewer scroll for it.
_COMPILE_ERROR = re.compile(
    r"^(/[^\s:]+:\d+:\d+:\s+error:\s+.*|.*\berror:\s+no such module.*)$",
    re.MULTILINE)


@dataclass
class TestSummary:
    verdict: str | None = None
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Frameworks that report COUNTS rather than names (pytest's summary line).
    # Kept apart from the name lists so `executed` stays a real number instead
    # of counting a synthesised placeholder as one test.
    unnamed_passed: int = 0

    @property
    def n_passed(self) -> int:
        return len(self.passed) + self.unnamed_passed

    @property
    def executed(self) -> int:
        return self.n_passed + len(self.failed)


def summarize_test_output(output: str) -> TestSummary:
    """Pull the verdict, the test roster and any compile errors out of a log."""
    s = TestSummary()
    text = output or ""
    for pattern, label in _VERDICTS:
        if pattern.search(text):
            s.verdict = label
            break

    for name, status in _XCTEST_CASE.findall(text):
        (s.passed if status == "passed" else s.failed).append(name)
    if not s.executed:
        for quoted, bare, status in _SWIFT_TESTING.findall(text):
            name = quoted or bare
            (s.passed if status == "passed" else s.failed).append(name)
    if not s.executed:
        s.failed.extend(_PYTEST_FAILED.findall(text))
        if (m := _PYTEST_SUMMARY.search(text)):
            # pytest names its failures but only counts its passes.
            s.unnamed_passed = int(m.group(2))

    seen: set[str] = set()
    for line in _COMPILE_ERROR.findall(text):
        line = line.strip()
        if line not in seen:
            seen.add(line)
            s.errors.append(line)
    return s


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
