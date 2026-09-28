"""The seam count honours the suite-level marker wherever the design puts it — DEV-907.

spec_47dec87b's design round 1 (DEV-312) wrote real seams for C1-C6 (C6 split
into C6a-d) and declared C7 suite-level on its SEAM line, as the design
instructions ask. Its checklist line for C7 carried no marker, and the count
check read "7 acceptance criteria need a seam but 6 were emitted".
"""
from coding_model_autonomous import design_testability as dt

_SEAM = "setup: `(none — pure function)` | act: ``f(1)`` | assert: ``#expect(f(1) == 2)``"


def _design(checklist: list[str], seams: list[str]) -> str:
    return ("# Architecture: Demo\n\n## Acceptance Criteria Checklist\n"
            + "".join(f"- [ ] {c}\n" for c in checklist)
            + "\n## Criterion Seams\n"
            + "".join(f"- {s}\n" for s in seams))


def _mismatch(md: str) -> list[str]:
    return [f.detail for f in dt.check_design_testability(md)
            if f.kind == dt.KIND_COUNT_MISMATCH]


# Round 1's shape: labelled checklist, marker on the seam line only.
ROUND_1 = _design(
    [f"C{i} — criterion {i}" for i in range(1, 7)]
    + ["C7 — HarnessTests.swift unmodified; full swift test suite passes with 7 tests total"],
    [f"C{i} | {_SEAM}" for i in range(1, 6)]
    + [f"C6{x} | {_SEAM}" for x in "abcd"]
    + ["C7 suite-level (no seam — build/test-run property; the suite reports 7 tests)"])


def test_a_seam_line_marker_exempts_its_criterion():
    assert _mismatch(ROUND_1) == []


def test_an_unlabelled_checklist_is_exempted_by_the_seam_marker():
    # Round 3's shape: the checklist lines carry no C-labels, the seams do.
    md = _design([f"criterion {i}" for i in range(1, 8)],
                 [f"C{i} | {_SEAM}" for i in range(1, 7)] + ["C7 | suite-level"])
    assert _mismatch(md) == []


def test_a_criterion_marked_on_both_sides_is_exempted_once():
    # C3 is suite-level on both lines; C2 has no seam at all, so one real seam
    # is still missing and the finding must still fire.
    md = _design(["C1 — a", "C2 — b", "C3 — suite-level property"],
                 [f"C1 | {_SEAM}", "C3 suite-level (no seam)"])
    details = _mismatch(md)
    assert details and details[0].startswith("2 acceptance criteria need a seam but 1")


def test_a_criterion_neither_marked_nor_seamed_still_fires():
    # The true-positive direction: marking C7 does not excuse a missing C6.
    md = _design([f"C{i} — criterion {i}" for i in range(1, 8)],
                 [f"C{i} | {_SEAM}" for i in range(1, 6)] + ["C7 suite-level (no seam)"])
    details = _mismatch(md)
    assert details and details[0].startswith("6 acceptance criteria need a seam but 5")


def test_a_checklist_marker_alone_still_exempts():
    # DEV-715's half keeps working.
    md = _design(["C1 — a", "C2 — suite-level: the suite passes"], [f"C1 | {_SEAM}"])
    assert _mismatch(md) == []
