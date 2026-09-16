"""DEV-635: edit-mode prompts must bound SEARCH anchor LENGTH, not just uniqueness.

Run 21 v3 (spec_67c3708b, DEV-602 split B) died this way. Four consecutive
implementer attempts emitted a SEARCH block spanning the entire 18-line
db.record_event payload literal in orchestrator_daemon.py, and every one failed
to reproduce it byte-exact. The unappliable-edit rotation (DEV-604) burned all
five attempts, synthesis salvaged files scoring 17% — below the 80% repair
threshold — and the spec failed terminally.

The instructions said byte-for-byte and exactly-once but never bounded the
anchor's length, and the uniqueness rule as written actively pushed models
toward LONGER anchors ("include more surrounding lines until the block is
unique") with no counterweight. A one-line unique anchor existed inside that
same literal; the model never needed eighteen.

The fix is prompt text, so the prompt text is what these tests pin. Both
edit-mode blocks carry it: the single-call block and the DEV-604 per-file
variant, because an implementer reaches for whichever one its mode selected.
"""
import pytest

from coding_model_autonomous.executor import (
    IMPLEMENTER_EDIT_MODE_INSTRUCTIONS, PER_FILE_EDIT_MODE_INSTRUCTIONS,
)

def flat(text: str) -> str:
    """The prompt blocks are hard-wrapped; a rule that spans a line break is
    still the rule. Assert against normalised whitespace, never raw newlines."""
    return " ".join(text.lower().split())


BOTH = pytest.mark.parametrize("instructions", [
    IMPLEMENTER_EDIT_MODE_INSTRUCTIONS,
    PER_FILE_EDIT_MODE_INSTRUCTIONS,
], ids=["single-call", "per-file"])


@BOTH
def test_anchor_length_is_bounded(instructions):
    assert "as short as uniqueness allows" in flat(instructions)


@BOTH
def test_a_concrete_target_range_is_given(instructions):
    # "Short" without a number is advice; a range is an instruction. The models
    # that failed run 21 were not being careless, they were optimising for the
    # only rule they had.
    assert "3-8 lines" in instructions


@BOTH
def test_the_cost_of_a_longer_anchor_is_stated(instructions):
    # The uniqueness rule pushes toward longer anchors, so the counterweight has
    # to say why that is expensive, not merely that shorter is preferred.
    assert "every extra line is another chance to mistype" in flat(instructions)


@BOTH
def test_a_short_interior_line_is_preferred_over_a_whole_literal(instructions):
    # The specific escape hatch run 21 needed: the 1-line unique anchor
    # ("passed": build_passed if not build_reason else False,) sat inside the
    # 18-line literal the model chose to transcribe whole.
    text = flat(instructions)
    assert "short unique interior line" in text
    assert "literal" in text


@BOTH
def test_insertions_have_their_own_anchoring_rule(instructions):
    # Insertions have no natural anchor, so without a rule the model invents a
    # large one. Anchor on the adjacent lines and re-emit them.
    text = flat(instructions)
    assert "2-3 adjacent lines" in text


@BOTH
def test_uniqueness_is_still_required(instructions):
    # The brevity rule is a counterweight, not a replacement: an anchor that is
    # short but ambiguous fails differently and just as hard.
    text = flat(instructions)
    assert "unique" in text


@BOTH
def test_byte_exactness_is_still_required(instructions):
    text = flat(instructions)
    assert "copy the anchor exactly" in text


def test_both_blocks_carry_the_rule_not_just_one():
    # The per-file variant (DEV-604) is a separate string that a different
    # dispatch path reaches. Fixing only the single-call block would leave the
    # exact mode run 21 died in untouched.
    assert IMPLEMENTER_EDIT_MODE_INSTRUCTIONS != PER_FILE_EDIT_MODE_INSTRUCTIONS
    for block in (IMPLEMENTER_EDIT_MODE_INSTRUCTIONS,
                  PER_FILE_EDIT_MODE_INSTRUCTIONS):
        assert "3-8 lines" in block
