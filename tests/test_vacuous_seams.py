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
