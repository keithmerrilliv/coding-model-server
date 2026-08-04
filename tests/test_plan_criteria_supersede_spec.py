"""The approved plan's acceptance criteria must reach the architect — DEV-490.

On spec_837b167f (2026-08-04) I rejected a plan over three acceptance criteria
the pipeline cannot evaluate: a two-phase before/after test run, a
`git status --porcelain` check of the runner worktree, and an exact count of
pre-existing compiler warnings. The planner removed all three and the plan was
approved — verified, the approved plan.yaml contained none of them.

The architect then reinstated all three verbatim, because `spec.md` still held
the original text (4 occurrences of "pre-fix") and `_render_plan_constraints`
threaded language, runtime, framework, dependencies, notes and clarifications
— but never `acceptance_criteria`. Nothing told the architect which document
won, so it followed the spec. A plan rejection survived exactly one stage.

This is the DEV-107 failure one field over, and that function's own docstring
already records the lesson: "Rejection notes alone did not hold — only
rewriting spec.md did."
"""
import textwrap

from coding_model_autonomous import executor as ex

PLAN = textwrap.dedent("""\
    title: "Fix the top-k logit mask"
    language: swift
    acceptance_criteria:
      - Post-fix build succeeds with zero errors and no new warnings.
      - ForcingStrategy.swift uses MLXArray(Float(-1e9)) as the else-branch.
      - New tests assert masked logits softmax to ~0.0 probability.
    constraints:
      dependencies_allowed: false
    """)

# The stale criteria that came back. Each is unevaluable — see DEV-483.
SPEC_WITH_STALE_CRITERIA = textwrap.dedent("""\
    # Fix the top-k logit mask

    ## Acceptance criteria
    - Pre-fix test run fails exit status 65 with maskPreservesIndices() failed.
    - git status --porcelain shows exactly M ForcingStrategy.swift.
    - Post-fix build contains exactly two pre-existing warning lines.
    """)


def _block(plan=PLAN):
    return ex._render_plan_constraints(plan)


# ── the criteria have to be in the prompt at all ─────────────────────────────

def test_approved_criteria_are_rendered():
    """The regression: they were simply absent."""
    block = _block()
    assert "MLXArray(Float(-1e9))" in block
    assert "softmax to ~0.0 probability" in block


def test_criteria_are_marked_definitive():
    block = _block()
    assert "definitive" in block.lower()


def test_the_architect_is_told_the_spec_may_be_stale():
    """Without this it has no reason to prefer one list over the other."""
    block = _block()
    assert "older set" in block
    assert "the spec was not rewritten" in block


def test_struck_criteria_are_named_as_deliberate():
    """The specific failure was reinstating what had been removed."""
    block = _block()
    assert "deliberately struck" in block
    assert "do not\nreinstate it" in block or "do not reinstate it" in block


def test_the_checklist_is_bounded_to_the_approved_list():
    assert "and nothing else" in _block()


# ── it has to survive into the actual architect message ──────────────────────

def test_criteria_reach_the_architect_message():
    msgs = ex.build_architect_message(SPEC_WITH_STALE_CRITERIA, plan_yaml=PLAN)
    user = msgs[1]["content"]
    assert "MLXArray(Float(-1e9))" in user


def test_the_approved_list_precedes_the_stale_spec():
    """Primacy: the architect must meet the real list before the old one."""
    user = ex.build_architect_message(SPEC_WITH_STALE_CRITERIA,
                                      plan_yaml=PLAN)[1]["content"]
    assert user.index("MLXArray(Float(-1e9))") < user.index("Pre-fix test run")


def test_precedence_is_restated_after_the_spec():
    """Recency matters as much as primacy — the spec is long and the stale
    criteria are the last thing read before the architect starts writing."""
    user = ex.build_architect_message(SPEC_WITH_STALE_CRITERIA,
                                      plan_yaml=PLAN)[1]["content"]
    tail = user[user.index("Pre-fix test run"):]
    assert "struck on purpose" in tail


# ── it must not fire when there is nothing to say ────────────────────────────

def test_a_plan_without_criteria_is_unchanged():
    """Pre-DEV-490 prompt, exactly."""
    block = _block("language: swift\n")
    assert block is not None
    assert "definitive" not in block.lower()


def test_a_plan_with_only_criteria_still_renders():
    """Previously returned None when nothing else was set, dropping them."""
    block = _block("acceptance_criteria:\n  - The suite passes.\n")
    assert block is not None
    assert "The suite passes." in block


def test_non_list_criteria_are_ignored_not_fatal():
    """Planner output is LLM-generated; a wrong shape drops the section rather
    than taking the run down — the same contract as every other field here."""
    assert _block("language: swift\nacceptance_criteria: 'all tests pass'\n") \
        is not None
    assert "definitive" not in _block(
        "language: swift\nacceptance_criteria: 'all tests pass'\n").lower()


def test_empty_entries_are_dropped():
    block = _block("acceptance_criteria:\n  - Real one.\n  - null\n  - ''\n")
    assert "Real one." in block
    assert "None" not in block


def test_unparseable_yaml_still_returns_none():
    assert ex._render_plan_constraints("{[not: valid") is None


def test_no_plan_means_no_block():
    """A spec run without an approved plan must be untouched."""
    user = ex.build_architect_message(SPEC_WITH_STALE_CRITERIA)[1]["content"]
    assert "definitive" not in user.lower()
