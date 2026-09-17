"""DEV-711: an evidence trail that does not resolve is not evidence.

On run 41 the reviewer wrote `tests/test_slice9_review.py` into a SWIFT
repository — six print statements and a comment explaining that the real tests
are in GameTests.swift — and then cited that file ten times, once per
acceptance criterion, for tests it does not contain.

The verdict was correct and the evidence was fabricated. That is the dangerous
shape: a wrong verdict gets caught by a red suite, a wrong citation gets caught
by nobody. DEV-513 made the gate header name test counts; this is the same
guarantee one level down.
"""

from coding_model_autonomous.executor import (
    REVIEWER_SYSTEM_PROMPT,
    _unresolvable_citations,
    parse_reviewer_response,
)

# Verbatim from run 41.
PLACEHOLDER = '''# Review notes - actual tests are XCTest in Tests/CentipedeCoreTests/GameTests.swift
# This placeholder exists because the orchestrator expects test files under tests/
print("Slice 9 acceptance criteria reviewed via static analysis")
print("- Spider.pointsNear=900, Spider.pointsMid=600 defined")
'''

REAL_PY = '''import pytest

def test_scores_by_distance():
    assert score(4) == 900

def test_shot_destroys_spider():
    assert destroyed is True
'''

REAL_SWIFT = '''import XCTest

final class GameTests: XCTestCase {
    func testSpiderScoredNearBand() { XCTAssertEqual(g.score, 900) }
    func testShotDestroysSpiderForPoints() { XCTAssertTrue(g.hit) }
}
'''


def test_run_41s_citation_is_unresolvable():
    bad = _unresolvable_citations(
        "- Shooting scores by distance -> "
        "tests/test_slice9_review.py::testSpiderScoredNearBand\n"
        "- amended -> tests/test_slice9_review.py::testShotDestroysSpiderForPoints",
        [("tests/test_slice9_review.py", PLACEHOLDER)])
    assert bad == ["tests/test_slice9_review.py::testSpiderScoredNearBand",
                   "tests/test_slice9_review.py::testShotDestroysSpiderForPoints"]


def test_a_real_pytest_citation_resolves():
    """Negative control."""
    assert _unresolvable_citations(
        "- criterion 1 -> tests/test_x.py::test_scores_by_distance",
        [("tests/test_x.py", REAL_PY)]) == []


def test_a_real_swift_citation_resolves():
    assert _unresolvable_citations(
        "- criterion 1 -> Tests/CoreTests/GameTests.swift::testSpiderScoredNearBand",
        [("Tests/CoreTests/GameTests.swift", REAL_SWIFT)]) == []


def test_a_file_the_reviewer_did_not_write_is_not_judged():
    """DEV-630: unknown is not wrong. An existing suite is not ours to resolve."""
    assert _unresolvable_citations(
        "- criterion 1 -> Tests/CoreTests/GameTests.swift::testSomethingElse",
        [("tests/test_mine.py", REAL_PY)]) == []


def test_no_written_files_means_no_finding():
    assert _unresolvable_citations("- c -> a.py::test_b", []) == []


# ── end to end through the parser ──────────────────────────────────────────

def _review(evidence, path, content):
    return parse_reviewer_response(
        f"<<<FILE: {path}>>>\n{content}<<<END_FILE>>>\n"
        "<<<REVIEW>>>\n"
        "## Code Review\n### Issues Found\n- none\n"
        "### Verdict\nPASS\n"
        f"### Verdict Evidence\n{evidence}\n"
        "<<<END_REVIEW>>>\n")


def test_a_fabricated_trail_downgrades_the_verdict():
    res = _review("- c1 -> tests/t.py::testNotThere", "tests/t.py", PLACEHOLDER)
    assert res.verdict == "FAIL"
    assert "does not define that test" in res.review_md
    assert "DEV-711" in res.review_md


def test_a_resolvable_trail_keeps_its_pass():
    """Negative control: the guard must not cost a correct reviewer an attempt."""
    res = _review("- c1 -> tests/t.py::test_scores_by_distance",
                  "tests/t.py", REAL_PY)
    assert res.verdict == "PASS"
    assert "DEV-711" not in res.review_md


# ── the belief that caused it ──────────────────────────────────────────────

def test_the_prompt_no_longer_demands_tests_slash_universally():
    assert "ALWAYS place test files\n    under a `tests/`" not in REVIEWER_SYSTEM_PROMPT


def test_the_prompt_tells_swift_reviewers_where_tests_go():
    assert "SwiftPM" in REVIEWER_SYSTEM_PROMPT
    assert "swift_test" in REVIEWER_SYSTEM_PROMPT


def test_the_prompt_does_not_assert_one_swift_layout():
    """Electric Sheep is an Xcode project: its tests live at the repo root in
    `ElectricSheepTests/`, NOT under `Tests/`. The first wording of this
    guidance asserted the SwiftPM layout as if it were universal, which would
    have pointed the reviewer at a directory the Xcode target does not
    contain — the DEV-601 failure mode, from the guidance meant to prevent it.
    """
    assert "do NOT guess one" in REVIEWER_SYSTEM_PROMPT
    assert "plan's declared output path always wins" in REVIEWER_SYSTEM_PROMPT


def test_the_prompt_forbids_a_placeholder():
    assert "NEVER write a placeholder file" in REVIEWER_SYSTEM_PROMPT


def test_the_prompt_says_to_extend_an_existing_test_file():
    assert "EXISTING test file" in REVIEWER_SYSTEM_PROMPT
