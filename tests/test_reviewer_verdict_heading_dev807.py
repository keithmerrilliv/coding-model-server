"""DEV-807: "the reviewer rejected this" and "the reviewer never said" differ.

Run 56's reviewer wrote a complete report — "No issues found", all six
acceptance criteria mapped to the test functions carrying them, over a suite
that passed 71 and failed 0 — and omitted one heading. `_VERDICT_RE` found no
match and the verdict defaulted to FAIL, so a review that objected to nothing
was recorded as a rejection. Only DEV-560's adjudication gate kept that from
discarding a correct first-attempt implementation and charging it a retry.

The fix is not to infer PASS from the silence; DEV-405 is why silence is not
approval either. It is to stop collapsing "no verdict" into the negative one,
which is the distinction DEV-629 exists to preserve. A missing heading is now
a ParseError, so the classifier re-runs the reviewer on its own budget.
"""
from coding_model_autonomous.executor import (
    REVIEWER_SYSTEM_PROMPT,
    ParseError,
    ReviewerResult,
    parse_reviewer_response,
)

EVIDENCE = "- criterion 1 -> tests/t.py::test_one"


def _review(body: str) -> "ReviewerResult | ParseError":
    return parse_reviewer_response(f"<<<REVIEW>>>\n{body}\n<<<END_REVIEW>>>\n")


# ── the defect ─────────────────────────────────────────────────────────────

def test_evidence_without_a_verdict_heading_is_no_verdict_not_a_fail():
    res = _review("## Code Review\n### Issues Found\nNo issues found.\n"
                  f"### Verdict Evidence\n{EVIDENCE}\n")
    assert isinstance(res, ParseError), (
        "a review stating no verdict must not be recorded as a FAIL verdict")


def test_the_reason_names_the_heading_that_was_actually_written():
    """The near miss is the whole diagnosis — say which one they typed."""
    res = _review("## Code Review\n### Issues Found\nNo issues found.\n"
                  f"### Verdict Evidence\n{EVIDENCE}\n")
    assert "### Verdict" in res.reason
    assert "Verdict Evidence" in res.reason
    assert "DEV-807" not in res.reason  # the reviewer needs the fix, not the ticket


def test_a_review_with_neither_heading_gets_the_simpler_reason():
    res = _review("## Code Review\n### Issues Found\nNo issues found.\n")
    assert isinstance(res, ParseError)
    assert "### Verdict" in res.reason
    assert "Verdict Evidence" not in res.reason


def test_run_56s_report_shape_is_caught():
    """The artifact that produced this ticket, heading for heading."""
    res = _review(
        "## Test Files Written\n"
        "- ElectricSheepTests/FailureRouterScopeTests.swift: criteria 1-4\n\n"
        "## Code Review\n"
        "### Issues Found\n"
        "No issues found.\n\n"
        "### Verdict Evidence\n"
        "- Criterion 1 -> FailureRouterScopeTests.swift::nestedScopes\n"
        "- Criterion 4 -> FailureRouterScopeTests.swift::concurrentScopes\n\n"
        "### Notes\n"
        "All acceptance criteria are satisfied by the delivered implementation.\n")
    assert isinstance(res, ParseError)


# ── the guards that must survive it ────────────────────────────────────────

def test_an_explicit_fail_is_still_a_fail():
    res = _review("## Code Review\n### Issues Found\n- critical a.py:1 - boom\n"
                  "### Verdict\nFAIL\n"
                  "### Verdict Evidence\n- critical a.py:1 - boom\n")
    assert isinstance(res, ReviewerResult)
    assert res.verdict == "FAIL"


def test_an_explicit_pass_with_evidence_still_passes():
    """Negative control: the new refusal must not cost a correct reviewer."""
    res = _review("## Code Review\n### Issues Found\nNo issues found.\n"
                  "### Verdict\nPASS\n"
                  f"### Verdict Evidence\n{EVIDENCE}\n")
    assert isinstance(res, ReviewerResult)
    assert res.verdict == "PASS"


def test_a_pass_without_evidence_is_still_downgraded_not_refused():
    """The DEV-711 family guard is unchanged: stated but unanchored is FAIL."""
    res = _review("## Code Review\n### Issues Found\nNo issues found.\n"
                  "### Verdict\nPASS\n")
    assert isinstance(res, ReviewerResult)
    assert res.verdict == "FAIL"
    assert "Verdict Evidence" in res.review_md


def test_a_missing_review_block_is_still_its_own_parse_error():
    res = parse_reviewer_response("no markers at all")
    assert isinstance(res, ParseError)
    assert "REVIEW" in res.reason


# ── the contract the model is given ────────────────────────────────────────

def test_the_prompt_says_the_two_headings_do_not_substitute():
    assert "neither replaces the other" in REVIEWER_SYSTEM_PROMPT
    assert "IN ADDITION TO" in REVIEWER_SYSTEM_PROMPT
