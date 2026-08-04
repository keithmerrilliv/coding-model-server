"""A near-miss opening delimiter must not discard a good design — DEV-498.

On run 5 of the Centipede spec (spec_9872c963, 2026-08-04) the architect emitted
`<<<DESINVARIANT>>>` instead of `<<<DESIGN>>>` on three of four consecutive
calls — DESIGN blended with INVARIANT, which the architect prompt shouts
throughout (rules 1 and 8). Everything else in those responses was correct: a
complete design body, a well-formed `<<<END>>>`, and a valid COMPLEXITY block.

The cost of discarding them is not one retry. Exhausting the parse attempts
fails the SPEC outright — no task retry, no synthesis:

    logger.error("spec %s: architect exhausted %d parse-retry attempt(s); "
                 "spec FAILED. ...")
    db.update_spec_status(spec.id, SpecStatus.FAILED)

So an approved plan and three rounds of design review can be lost to one
mistyped token.
"""
import pathlib

import pytest

from coding_model_autonomous.executor import (
    ArchitectResult, ParseError, parse_architect_response,
)

BODY = """\
# Architecture: Centipede Logic Core

## Overview
Deterministic Swift simulation.
"""
COMPLEXITY = """\
<<<COMPLEXITY>>>
Tier: high
Recommended agent: deep_implementer
Justification: many files, subtle invariants.
<<<END_COMPLEXITY>>>
"""


def _resp(opening):
    return f"{opening}\n{BODY}<<<END>>>\n\n{COMPLEXITY}"


# ── the exact corruption seen in production ─────────────────────────────────

def test_desinvariant_is_recovered():
    """The regression."""
    r = parse_architect_response(_resp("<<<DESINVARIANT>>>"))
    assert isinstance(r, ArchitectResult)
    assert "Centipede Logic Core" in r.design_md


def test_the_complexity_block_still_parses_after_recovery():
    """It sits outside the design block and must be unaffected."""
    r = parse_architect_response(_resp("<<<DESINVARIANT>>>"))
    assert r.complexity["tier"] == "high"
    assert r.complexity["recommended_agent"] == "deep_implementer"


@pytest.mark.parametrize("opening", [
    "<<<DESIGN>>>",        # the correct one, must still work
    "<<<DESINVARIANT>>>",  # observed
    "<<<DESIGNS>>>",       # plausible pluralisation
    "<<<DES>>>",           # truncation
    "<<<DESIGN_DOC>>>",    # underscore variant
])
def test_openings_starting_with_des_are_accepted(opening):
    r = parse_architect_response(_resp(opening))
    assert isinstance(r, ArchitectResult), f"{opening} should parse"
    assert "Centipede Logic Core" in r.design_md


# ── it must not become a catch-all ──────────────────────────────────────────

@pytest.mark.parametrize("opening", [
    "<<<COMPLEXITY>>>",   # a different block entirely
    "<<<PLAN>>>",
    "<<<IMPLEMENTATION>>>",
])
def test_unrelated_delimiters_are_still_rejected(opening):
    """Recovering `DES*` must not turn the parser into 'accept anything', or a
    COMPLEXITY block alone would be mistaken for a design."""
    assert isinstance(parse_architect_response(_resp(opening)), ParseError)


def test_no_delimiters_at_all_is_still_an_error():
    assert isinstance(parse_architect_response("just prose, no markers"),
                      ParseError)


def test_an_empty_recovered_block_is_still_an_error():
    assert isinstance(parse_architect_response("<<<DESINVARIANT>>>\n\n<<<END>>>"),
                      ParseError)


# ── the failure message must name what was actually there ───────────────────

def test_the_error_names_the_delimiters_it_saw():
    """"No block found" reads as "the model produced nothing usable", which is
    what sent me to the artifact to find a complete design behind one token."""
    err = parse_architect_response("<<<PLAN>>>\nstuff\n<<<END>>>")
    assert isinstance(err, ParseError)
    assert "PLAN" in err.reason, err.reason


def test_the_error_says_none_when_there_are_no_delimiters():
    err = parse_architect_response("just prose")
    assert isinstance(err, ParseError)
    assert "none" in err.reason


# ── replay of the real discarded response ───────────────────────────────────

FAILED = pathlib.Path(
    "var/tasks_db/specs/spec_9872c963/architect_failed_response_attempt1.txt")


@pytest.mark.skipif(not FAILED.is_file(),
                    reason="run 5 artifacts not present on this machine")
def test_the_real_discarded_design_is_recovered():
    """The whole point: this exact response was thrown away in production."""
    raw = FAILED.read_text().split("\n", 2)[2]   # drop the error header
    r = parse_architect_response(raw)
    assert isinstance(r, ArchitectResult), "the real response must now parse"
    assert len(r.design_md) > 5000, "a complete design, not a fragment"
    assert r.complexity is not None
