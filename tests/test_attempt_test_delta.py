"""DEV-700: an attempt that adds zero tests must not read as "the suite passed".

Run 39's implementer produced all three planned files, correctly implemented
the feature, correctly updated one stale pre-existing test — and wrote ZERO new
tests. 26 test functions before, 26 after. The gate said:

    Build check: **compiled and the suite passed.**

True of the 26 pre-existing tests, and it told the reviewer nothing about the
slice. DEV-645's planned-output check was satisfied because every declared file
existed; the missing deliverable was INSIDE a file that did exist.
"""

import json

import pytest

from coding_model_server.orchestrator_daemon import (
    _test_delta_line,
    required_new_test_count,
    attempt_test_delta,
)

BEFORE_SWIFT = """import XCTest
final class GameTests: XCTestCase {
    func testAlpha() {}
    func testBeta() {}
}
"""
ADDS_TWO = BEFORE_SWIFT.replace(
    "    func testBeta() {}",
    "    func testBeta() {}\n    func testGamma() {}\n    func testDelta() {}")
CHANGES_AN_ASSERTION = BEFORE_SWIFT.replace("func testAlpha() {}",
                                            "func testAlpha() { XCTAssertEqual(g.score, 900) }")


def _spec_dir(tmp_path, editable):
    (tmp_path / "context.json").write_text(json.dumps({
        "spec_id": "s1", "repo": "electric-sheep", "base_ref": "main",
        "candidates": list(editable), "declared": [], "protected_paths": [],
        "editable": editable, "protected": {}, "omitted": [],
        "fetched_at": "t", "fetched_by": "test", "fetches": 1,
    }))
    return tmp_path


# ── the count the spec asks for ────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("at least 5 new tests covering criteria 1-5", 5),
    ("Add 6 new tests for the bridge", 6),
    (">= 3 new tests", 3),
    ("a minimum of 4 new tests", 4),
    ("no numeric requirement here", None),
    ("", None),
])
def test_required_new_test_count(text, expected):
    assert required_new_test_count(text) == expected


# ── the delta ──────────────────────────────────────────────────────────────

def test_attempt_that_adds_no_tests_measures_zero(tmp_path):
    """Run 39's shape: the file changed, the test count did not."""
    d = _spec_dir(tmp_path, {"Tests/GameTests.swift": BEFORE_SWIFT})
    delta = attempt_test_delta(
        d, [("Tests/GameTests.swift", CHANGES_AN_ASSERTION)], "swift_test")
    assert sum(delta.values()) == 0


def test_added_tests_are_counted(tmp_path):
    d = _spec_dir(tmp_path, {"Tests/GameTests.swift": BEFORE_SWIFT})
    delta = attempt_test_delta(
        d, [("Tests/GameTests.swift", ADDS_TWO)], "swift_test")
    assert delta == {"Tests/GameTests.swift": 2}


def test_no_context_means_could_not_tell(tmp_path):
    """DEV-630: absent is not zero. Never report a count we did not measure."""
    assert attempt_test_delta(
        tmp_path, [("Tests/GameTests.swift", ADDS_TWO)], "swift_test") is None


# ── the gate line ──────────────────────────────────────────────────────────

def test_zero_added_is_flagged_on_the_gate(tmp_path):
    d = _spec_dir(tmp_path, {"Tests/GameTests.swift": BEFORE_SWIFT})
    line = _test_delta_line(d, [("Tests/GameTests.swift", CHANGES_AN_ASSERTION)],
                            "swift_test", "at least 5 new tests are required")
    assert "Tests added: 0" in line
    assert "at least 5" in line
    assert "DEV-700" in line


def test_a_shortfall_is_flagged(tmp_path):
    d = _spec_dir(tmp_path, {"Tests/GameTests.swift": BEFORE_SWIFT})
    line = _test_delta_line(d, [("Tests/GameTests.swift", ADDS_TWO)],
                            "swift_test", "at least 5 new tests")
    assert "⚠" in line and "3 short" in line


def test_meeting_the_requirement_is_not_flagged(tmp_path):
    """Negative control — a good attempt gets a plain count, no warning."""
    d = _spec_dir(tmp_path, {"Tests/GameTests.swift": BEFORE_SWIFT})
    line = _test_delta_line(d, [("Tests/GameTests.swift", ADDS_TWO)],
                            "swift_test", "at least 2 new tests")
    assert "Tests added: **2**" in line
    assert "⚠" not in line


def test_nothing_is_claimed_when_it_cannot_be_measured(tmp_path):
    """No context.json — the line must be empty, not 'Tests added: 0'."""
    assert _test_delta_line(tmp_path, [("a.swift", ADDS_TWO)],
                            "swift_test", "5 new tests") == ""


def test_no_framework_means_no_line(tmp_path):
    d = _spec_dir(tmp_path, {"Tests/GameTests.swift": BEFORE_SWIFT})
    assert _test_delta_line(d, [("Tests/GameTests.swift", ADDS_TWO)],
                            None, "5 new tests") == ""


def test_pytest_targets_are_counted_too(tmp_path):
    before = "def test_a():\n    pass\n"
    after = before + "\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n"
    d = _spec_dir(tmp_path, {"tests/test_x.py": before})
    delta = attempt_test_delta(d, [("tests/test_x.py", after)], "pytest")
    assert delta == {"tests/test_x.py": 2}
