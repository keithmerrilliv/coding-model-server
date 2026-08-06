"""A seam step that names no call is not a seam (DEV-523).

Run 7's design emitted a complete `## Criterion Seams` section on the first
attempt — 16 criteria, 16 seams, **0 findings** — and four of those criteria
had no reachable setup at all. That is the exact class DEV-481 was built to
catch, walking past the gate built to catch it.

The reason is structural, not a missing rule. Every rule in this module reads
backticked spans: `_check_symbols` resolves `Type.member`, `_check_readonly`
looks for an assignment, `_check_equatable` needs a `==`. A step written as
English prose — "place single-segment chain at rightmost column" — offers none
of them, so every rule fails open and the seam records as complete. So this
check is not one more rule beside the others; it is the precondition that
makes them reachable.

Scope is deliberately narrow (DEV-440): "this step contains no backticked
span" is not a judgement call, which is what makes it safe to enforce against
a retry budget shared with human rejections and DEV-468's routing.
"""
from pathlib import Path

import pytest

from coding_model_autonomous import design_testability as dt

RUN7 = Path("var/tasks_db/specs/spec_9e190582/design.md")


def _seam(setup="`a.b()`", act="`c.d()`", assert_="`e == f`"):
    return dt.Seam(criterion="C", setup=setup, act=act, assert_=assert_)


class TestProseIsNotASeam:
    @pytest.mark.parametrize("field", ["setup", "act", "assert_"])
    def test_a_prose_step_is_a_finding(self, field):
        seam = _seam(**{field: "place a chain at the rightmost column"})
        kinds = [f.kind for f in dt._check_names_a_call(seam)]
        assert dt.KIND_PROSE_SEAM in kinds

    def test_a_seam_naming_calls_throughout_is_silent(self):
        assert dt._check_names_a_call(_seam()) == []

    def test_an_empty_step_is_left_to_the_completeness_rule(self):
        """Seam.missing() already reports it; two findings for one defect
        reads as two defects."""
        kinds = [f.kind for f in dt._check_names_a_call(_seam(setup="  "))]
        assert dt.KIND_PROSE_SEAM not in kinds

    def test_the_finding_names_which_steps(self):
        seam = _seam(setup="do a thing", assert_="observe a thing")
        detail = dt._check_names_a_call(seam)[0].detail
        assert "setup and assert" in detail


class TestElidedSteps:
    """`let snapshot = ...` carries a span, so requiring a span alone does not
    reach it — run 7's criterion 15 was exactly this shape."""

    def test_assignment_from_an_ellipsis_is_a_placeholder(self):
        seam = _seam(setup="`let snapshot = ...`")
        assert [f.kind for f in dt._check_names_a_call(seam)] == \
            [dt.KIND_ELIDED_STEP]

    def test_a_bare_ellipsis_is_a_placeholder(self):
        assert [f.kind for f in dt._check_names_a_call(_seam(act="`...`"))] == \
            [dt.KIND_ELIDED_STEP]

    def test_a_call_with_elided_arguments_is_not_a_placeholder(self):
        """`world.step(...)` names the call; the arguments are not the point."""
        assert dt._check_names_a_call(_seam(act="`world.step(...)`")) == []

    def test_one_real_span_beside_a_placeholder_is_accepted(self):
        seam = _seam(setup="`let w = GameWorld(seed: 1)`; `let s = ...`")
        assert dt._check_names_a_call(seam) == []


@pytest.mark.skipif(not RUN7.is_file(), reason="run 7 artifacts not present")
class TestAgainstRun7:
    """The design that produced 16 seams and 0 findings."""

    def test_the_check_no_longer_reports_nothing(self):
        findings = dt.check_design_testability(RUN7.read_text())
        prose = [f for f in findings
                 if f.kind in (dt.KIND_PROSE_SEAM, dt.KIND_ELIDED_STEP)]
        assert len(prose) == 8, [f.criterion for f in prose]

    def test_it_flags_the_unreachable_setup_the_ticket_names(self):
        """Criterion 14 — "reduce a chain to one segment" — names no API, and
        `GameWorld` offers none that could do it."""
        findings = dt.check_design_testability(RUN7.read_text())
        assert any(f.criterion.startswith("Criterion 14")
                   and f.kind == dt.KIND_PROSE_SEAM for f in findings)

    def test_seams_that_name_calls_stay_silent(self):
        """Half the design's seams are well-formed and must not be touched —
        this is the DEV-440 ceiling on how strict the rule can get."""
        findings = dt.check_design_testability(RUN7.read_text())
        flagged = {f.criterion for f in findings}
        seams = dt.parse_seams(RUN7.read_text())
        criteria = dt.parse_checklist(RUN7.read_text())
        assert len(seams) == len(criteria) == 16
        assert len(flagged) == 8  # the other 8 name their calls
