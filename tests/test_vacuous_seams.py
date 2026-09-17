"""DEV-710: a seam that is syntactically valid and does nothing is not a seam.

DEV-523's prose_seam rule asks only whether a backticked span EXISTS, never
whether it does anything. On run 41 the architect found the gap: under repeated
rejection the cheapest way to clear a syntactic rule is a syntactically valid
nullity. It replaced two CORRECT seams with `act: _ = "testName" | assert:
true`. The check made the design worse.

The second half matters as much as the first. Not every criterion has a call —
"at least 6 new tests exist and the pre-existing ones still pass" is a property
of the suite. With nowhere for those to go, rejecting fix 1 alone would make the
design unapprovable and teach exactly the behaviour it is meant to stop.
"""

import pytest

from coding_model_autonomous.design_testability import (
    KIND_PROSE_SEAM,
    KIND_VACUOUS_SEAM,
    Seam,
    _check_names_a_call,
    _is_vacuous_span,
    is_suite_level,
)


def _kinds(seam):
    return {f.kind for f in _check_names_a_call(seam)}


# ── the vocabulary ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("span", [
    "true", "false", "True", "False", "nil", "None", "()",
    "_ = ()", "_ = GameTests.self", "_ = GameTests.class",
    '_ = "testShotDestroysSpiderForPoints"', '"testName"', "'testName'",
    "pass", "assert True",
])
def test_vacuous_spans_are_recognised(span):
    assert _is_vacuous_span(span)


@pytest.mark.parametrize("span", [
    "g.fire(); g.tick()",
    "g.state.score == Spider.pointsMid",
    "XCTAssertTrue(g.isOver)",
    "world.place(chain, at: Position(col: 29, row: 0))",
    "let x = trueValue()",
    "w.removeMushroom(at: p)",
    '_ = try parse("x")',
    "bridge.update(from: engine)",
])
def test_real_spans_are_not_vacuous(span):
    """The narrowness control. A false finding here costs an architect round."""
    assert not _is_vacuous_span(span)


# ── the finding ────────────────────────────────────────────────────────────

def test_run_41s_actual_seam_is_now_a_finding():
    """Verbatim from the accepted design on run 41."""
    seam = Seam(criterion="testShotDestroysSpiderForPoints amended",
                setup="`_ = ()`",
                act='`_ = "testShotDestroysSpiderForPoints"`',
                assert_="`true`")
    assert KIND_VACUOUS_SEAM in _kinds(seam)


def test_the_metatype_form_is_a_finding():
    seam = Seam(criterion=">=6 new tests & existing pass unchanged",
                setup="`_ = ()`", act="`_ = GameTests.self`", assert_="`true`")
    assert KIND_VACUOUS_SEAM in _kinds(seam)


def test_run_41s_correct_earlier_seam_still_passes():
    """The pass-2 seam the architect replaced. It must never have been flagged."""
    seam = Seam(
        criterion="Criterion 4",
        setup=("`var g = Game(world: World(mushrooms: [:], chains: []), "
               "gameState: GameState(), playerPosition: Position(column: 15, "
               "row: 29))`"),
        act="`g.fire(); g.tick()`",
        assert_="`g.state.score == Spider.pointsMid`")
    assert _check_names_a_call(seam) == []


def test_a_vacuous_setup_alone_is_not_a_finding():
    """`_ = ()` is a legitimate way to say no state is needed."""
    seam = Seam(criterion="c", setup="`_ = ()`", act="`g.tick()`",
                assert_="`g.isOver == true`")
    assert KIND_VACUOUS_SEAM not in _kinds(seam)


def test_prose_is_still_prose():
    """DEV-523's rule must keep firing — this replaces nothing."""
    seam = Seam(criterion="c", setup="place a chain at the rightmost column",
                act="step the world", assert_="the chain has split")
    assert KIND_PROSE_SEAM in _kinds(seam)


def test_the_finding_tells_the_architect_about_the_escape_hatch():
    """Without this the rule just teaches a better fake."""
    seam = Seam(criterion="c", setup="`_ = ()`", act="`_ = X.self`",
                assert_="`true`")
    detail = next(f.detail for f in _check_names_a_call(seam)
                  if f.kind == KIND_VACUOUS_SEAM)
    assert "suite-level" in detail


# ── the escape hatch ───────────────────────────────────────────────────────

@pytest.mark.parametrize("criterion", [
    "suite-level: at least 6 new tests exist",
    "at least 6 new tests exist (suite_level)",
    "meta-criterion — the pre-existing tests still pass",
    "no-seam: the suite grows by 6 cases",
])
def test_a_criterion_can_declare_itself_suite_level(criterion):
    assert is_suite_level(Seam(criterion=criterion, setup="", act="",
                               assert_=""))


def test_an_ordinary_criterion_is_not_suite_level():
    """Negative control — the marker must be deliberate."""
    assert not is_suite_level(
        Seam(criterion="Shooting a spider scores by distance",
             setup="`var g = Game()`", act="`g.fire()`",
             assert_="`g.score == 900`"))


# ── DEV-715: the hatch must be reachable from the prompt ───────────────────

def test_the_architect_prompt_names_the_suite_level_marker():
    """Run 1 of the ES queue proved the hatch was unreachable.

    DEV-710 added `suite-level` and advertised it only inside the
    `vacuous_seam` finding message — a finding that never fires for the case
    the marker is for. The architect wrote `xcodebuild_build(...)`,
    `XCTestSuite.allTestCases(...)` and `git_diff(...)` instead, none of which
    exist, and all of which pass every seam rule.
    """
    from coding_model_autonomous.executor import ARCHITECT_SYSTEM_PROMPT
    assert "suite-level" in ARCHITECT_SYSTEM_PROMPT
    assert "property of the build" in ARCHITECT_SYSTEM_PROMPT


def test_the_prompt_names_the_invented_calls_it_must_not_write():
    """The three real examples, so the instruction is concrete."""
    from coding_model_autonomous.executor import ARCHITECT_SYSTEM_PROMPT
    for invented in ("xcodebuild_build", "allTestCases", "git_diff"):
        assert invented in ARCHITECT_SYSTEM_PROMPT, invented


def test_the_prompt_still_demands_an_api_fix_for_code_criteria():
    """Negative control: the hatch must not become an excuse.

    A criterion that IS about the code and cannot be reached is still a defect
    to fix in the API, not something to mark suite-level.
    """
    from coding_model_autonomous.executor import ARCHITECT_SYSTEM_PROMPT
    assert "fix the API" in ARCHITECT_SYSTEM_PROMPT
    assert "ABOUT THE CODE" in ARCHITECT_SYSTEM_PROMPT


# ── DEV-715: the count check must not punish the hatch ─────────────────────

def _design(criteria, seams):
    return ("## Acceptance Criteria Checklist\n\n"
            + "\n".join(f"- {c}" for c in criteria)
            + "\n\n## Criterion Seams\n\n"
            + "\n".join(f"- {s}" for s in seams) + "\n")


REAL_SEAM = ("{n} | setup: `var g = Game()` | act: `g.tick()` | "
             "assert: `g.count == 1`")


def test_suite_level_criteria_are_not_counted_against_the_seams():
    """Run 2's architect did exactly what DEV-715's prompt told it to.

    It marked three criteria `(suite-level)`, gave them no seams, and wrote
    three real seams for the other three. The count check compared 3 seams
    against 6 criteria and rejected a correct design. The prompt half of the
    hatch shipped without this half.
    """
    from coding_model_autonomous.design_testability import (
        KIND_COUNT_MISMATCH, check_design_testability)
    md = _design(
        ["Build succeeds with no new warnings. (suite-level — build property)",
         "Double-update spawns 3 particles",
         "Capacity clamp consumes 5",
         "Cursor resets on forcer replacement",
         "This file establishes its suite. (suite-level)",
         "The visionOS branch is UNCHANGED. (suite-level — diff property)"],
        [REAL_SEAM.format(n="Double-update spawns 3 particles"),
         REAL_SEAM.format(n="Capacity clamp consumes 5"),
         REAL_SEAM.format(n="Cursor resets on forcer replacement")])
    assert KIND_COUNT_MISMATCH not in {f.kind for f in
                                       check_design_testability(md)}


def test_a_genuine_count_mismatch_still_fires():
    """Negative control — the hatch must not disable the rule."""
    from coding_model_autonomous.design_testability import (
        KIND_COUNT_MISMATCH, check_design_testability)
    md = _design(
        ["Build succeeds. (suite-level)", "Criterion A", "Criterion B"],
        [REAL_SEAM.format(n="Criterion A")])          # B has no seam
    kinds = {f.kind for f in check_design_testability(md)}
    assert KIND_COUNT_MISMATCH in kinds


def test_the_mismatch_message_says_how_many_were_skipped():
    """So a human reading the gate knows the marker was honoured."""
    from coding_model_autonomous.design_testability import (
        KIND_COUNT_MISMATCH, check_design_testability)
    md = _design(
        ["Build succeeds. (suite-level)", "Criterion A", "Criterion B"],
        [REAL_SEAM.format(n="Criterion A")])
    detail = next(f.detail for f in check_design_testability(md)
                  if f.kind == KIND_COUNT_MISMATCH)
    assert "suite-level" in detail and "correctly have none" in detail
