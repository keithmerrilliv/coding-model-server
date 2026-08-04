"""The design must afford the tests its own checklist demands — DEV-481.

Run 4 of the Centipede spec (spec_8dac1142, 2026-08-04) produced a design that
satisfied every existing rule: all 18 spec criteria were covered, and each was
restated as a concrete assertion, which is what rule 9 asks for. Synthesis then
implemented it and every source file compiled. It still failed, on three errors
that were all in test files and all the same root cause — the API could not
carry out the criteria the design itself listed:

    WorldStateTests.swift:86:22  'chains' setter is inaccessible
    WorldStateTests.swift:285:41 binary operator '==' cannot be applied to two
                                 'HitOutcome' operands
    MushroomFieldTests.swift:46:26 type 'Field' has no member
                                   'bottomPlayerZoneStart'

`chains` was `public private(set)` with only `init(seed:)`, so the fall-off
criterion had no way to place a chain. `HitOutcome` had associated values and
no Equatable, so the empty-hit criterion could not be asserted. The player-zone
bound was prose with no named constant.

The distinction these tests pin is the one that was missed: a criterion can be
phrased perfectly (rule 9) and still be impossible to execute (rule 10).
"""
import re

from coding_model_autonomous import executor as ex


# ── the architect is told to check affordance, separately from phrasing ──────

def test_architect_has_an_affordance_rule_distinct_from_rule_9():
    p = ex.ARCHITECT_SYSTEM_PROMPT
    assert "AFFORDS" in p, "the architect must be told the design has to afford its tests"
    # rule 9 (phrasing) must still be there — this adds to it, never replaces it
    assert "TESTABLE" in p
    nine = p.index("Make the Acceptance Criteria Checklist TESTABLE")
    ten = p.index("AFFORDS")
    assert ten > nine, "affordance is the follow-on to rule 9, not a rewrite of it"


def test_architect_is_told_the_design_is_at_fault_not_the_criterion():
    """The failure mode is dropping the criterion instead of adding the seam."""
    p = ex.ARCHITECT_SYSTEM_PROMPT
    assert re.search(r"DESIGN is wrong, not\s+the criterion", p)


def test_architect_names_the_three_steps_a_test_needs():
    p = ex.ARCHITECT_SYSTEM_PROMPT
    for step in ("construct", "invoke", "observe"):
        assert step in p, f"the walk-through must name '{step}'"


def test_architect_names_each_seam_run_4_was_missing():
    """One clause per error run 4 actually produced."""
    p = ex.ARCHITECT_SYSTEM_PROMPT
    assert "Equatable" in p                      # HitOutcome == .empty
    assert "initialiser" in p                    # chains had only init(seed:)
    assert "name every constant" in p            # Field.bottomPlayerZoneStart


# ── the design reviewer is told to look for it too ───────────────────────────

def test_design_review_checks_affordance():
    p = ex.DESIGN_REVIEW_SYSTEM_PROMPT
    assert "AFFORDANCE" in p


def test_design_review_keeps_affordance_separate_from_testability():
    """Collapsing them is how run 4 passed: coverage and phrasing were fine."""
    p = ex.DESIGN_REVIEW_SYSTEM_PROMPT
    assert "TESTABILITY" in p and "AFFORDANCE" in p
    assert p.index("TESTABILITY") < p.index("AFFORDANCE")
    assert "separately from whether a criterion is well phrased" in p


def test_design_review_is_told_these_are_design_defects():
    p = ex.DESIGN_REVIEW_SYSTEM_PROMPT
    assert "These are DESIGN" in p and "defects" in p


def test_design_review_asks_which_criterion_is_stranded():
    """A verdict naming no criterion is not actionable for the architect."""
    assert "stranded" in ex.DESIGN_REVIEW_SYSTEM_PROMPT


def test_design_review_still_defaults_to_pass():
    """A new check must not become a source of false FAILs — see DEV-440,
    where the automated review already fails designs over non-defects."""
    assert "Default to PASS" in ex.DESIGN_REVIEW_SYSTEM_PROMPT


# ── the prompts still hold together ──────────────────────────────────────────

def test_the_prompts_are_still_well_formed():
    for p in (ex.ARCHITECT_SYSTEM_PROMPT, ex.DESIGN_REVIEW_SYSTEM_PROMPT):
        assert p.strip()
        assert "<<<" in p, "the delimiters the parsers key on must survive"


def test_architect_rules_are_still_sequentially_numbered():
    """A hand-edited numbered list is easy to break."""
    nums = [int(m) for m in re.findall(r"^(\d+)\. ", ex.ARCHITECT_SYSTEM_PROMPT,
                                       re.MULTILINE)]
    assert nums == list(range(1, len(nums) + 1)), f"rule numbering broken: {nums}"
    assert len(nums) >= 10
