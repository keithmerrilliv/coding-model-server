"""DEV-507: a corrupted manifest delimiter must not cost an implementation attempt.

DEV-498 established that these models corrupt their own opening delimiter
reproducibly — <<<DESINVARIANT>>> for <<<DESIGN>>> on three consecutive
architect calls — and fixed it for the architect. The same corruption class
lands on the implementer's <<<MANIFEST>>> block, where run 6 of DEV-102 showed
it is handled worse: the failure propagated straight to the caller's rotation
retry, spending one of MAX_RETRIES and rotating moe_implementer ->
fast_implementer, two tiers below the architect's recommendation, for a defect
that had nothing to do with the agent's capability.

Three behaviours are pinned here: the evidence survives, a near-miss opening
delimiter is recovered, and a parse failure spends a parse retry rather than an
implementation attempt.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import (
    ImplementerResult, ManifestResult, ParseError, parse_manifest_response,
)

BODY = "shared/t.ts | contract types | T\n"
GOOD = f"<<<MANIFEST>>>\n{BODY}<<<END_MANIFEST>>>"
FILE_T = "<<<FILE: shared/t.ts>>>\nexport type T = number;\n<<<END_FILE>>>"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer",
                          title="impl demo")
    return db.get_spec(spec.id), db.get_task(task.id), db.spec_dir(spec.id)


# ── The parser itself ────────────────────────────────────────────────────────

def test_exact_delimiter_still_parses():
    res = parse_manifest_response(GOOD)
    assert isinstance(res, ManifestResult)
    assert [e.path for e in res.entries] == ["shared/t.ts"]


@pytest.mark.parametrize("opener", [
    "<<<MANIFESTO>>>",      # blended with prompt vocabulary
    "<<<MANIFEST_LIST>>>",
    "<<<MANFILE>>>",
    "<<MANIFST>>",          # corrupted AND short-bracketed
])
def test_near_miss_opening_delimiter_is_recovered(opener):
    raw = f"{opener}\n{BODY}<<<END_MANIFEST>>>"
    res = parse_manifest_response(raw)
    assert isinstance(res, ManifestResult), f"{opener} should have been recovered"
    assert [e.path for e in res.entries] == ["shared/t.ts"]


def test_recovery_requires_an_opener_that_starts_with_man():
    # The fallback is deliberately narrow: MAN-prefixed openers only. A
    # delimiter that shares no prefix is a different failure and must not be
    # silently reinterpreted as a manifest.
    raw = f"<<<FILELIST>>>\n{BODY}<<<END_MANIFEST>>>"
    assert isinstance(parse_manifest_response(raw), ParseError)


def test_recovery_still_requires_the_closing_delimiter():
    # END_MANIFEST is unchanged in every observed corruption, so it is what
    # bounds the block. Without it there is no unambiguous end.
    assert isinstance(parse_manifest_response(f"<<<MANIFESTO>>>\n{BODY}"), ParseError)


def test_end_manifest_is_never_mistaken_for_an_opener():
    # END_MANIFEST starts with END, not MAN, so the fuzzy opener cannot match it
    # and swallow the response from the closing tag onward.
    res = parse_manifest_response(GOOD)
    assert isinstance(res, ManifestResult)
    assert all("END_MANIFEST" not in e.path for e in res.entries)


def test_parse_error_names_the_delimiters_that_were_present():
    # "no block found" reads as "the model produced nothing usable", which is
    # what sent me to the artifact to find a complete response behind one wrong
    # token. Name what was actually there.
    err = parse_manifest_response("<<<PLAN>>>\nstuff\n<<<END_PLAN>>>")
    assert isinstance(err, ParseError)
    assert "PLAN" in err.reason


def test_parse_error_without_delimiters_says_so_plainly():
    err = parse_manifest_response("I could not do this, sorry.")
    assert isinstance(err, ParseError)
    assert "delimiters present" not in err.reason


def test_parse_error_keeps_the_raw_response():
    raw = "I could not do this, sorry."
    err = parse_manifest_response(raw)
    assert isinstance(err, ParseError)
    assert err.raw == raw


# ── The parse-retry budget ───────────────────────────────────────────────────

def test_manifest_parse_retries_defaults_to_the_architect_budget():
    assert executor.MANIFEST_PARSE_RETRIES == executor.ARCHITECT_PARSE_RETRIES == 2


def test_a_parse_failure_is_retried_rather_than_rotated(db, spec_task):
    spec, task, spec_dir = spec_task
    bad = "I forgot the manifest markers"
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent",
                               side_effect=[bad, GOOD, FILE_T]) as ca:
            res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                           "implementer", [], None)
    assert isinstance(res, ImplementerResult)
    assert [p for p, _ in res.files] == ["shared/t.ts"]
    assert ca.call_count == 3  # failed manifest + retried manifest + 1 per file


def test_a_parse_failure_does_not_consume_an_implementer_attempt(db, spec_task):
    # The whole point: run 6 of DEV-102 lost one of five attempts to a
    # delimiter. The retry count must be untouched by a parse failure.
    spec, task, spec_dir = spec_task
    before = task.retry_count
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent",
                               side_effect=["bad", GOOD, FILE_T]):
            d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                     "implementer", [], None)
    assert db.get_task(task.id).retry_count == before


def test_the_agent_is_not_rotated_between_parse_retries(db, spec_task):
    # A response that could not be read says nothing about whether this agent
    # can do the work (DEV-431), so the retry goes back to the same agent.
    spec, task, spec_dir = spec_task
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent",
                               side_effect=["bad", GOOD, FILE_T]) as ca:
            d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                     "moe_implementer", [], None)
    agents = [c.kwargs.get("agent") for c in ca.call_args_list[:2]]
    assert agents == ["moe_implementer", "moe_implementer"]


def test_an_exhausted_parse_budget_propagates(db, spec_task):
    spec, task, spec_dir = spec_task
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent", side_effect=["bad"] * 3) as ca:
            res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                           "implementer", [], None)
    assert isinstance(res, ParseError)
    assert ca.call_count == 3


def test_a_zero_retry_budget_propagates_immediately(db, spec_task):
    spec, task, spec_dir = spec_task
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 0):
        with mock.patch.object(d, "call_agent", side_effect=["bad"]) as ca:
            res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                           "implementer", [], None)
    assert isinstance(res, ParseError)
    assert ca.call_count == 1


# ── The evidence ─────────────────────────────────────────────────────────────

def test_every_failed_response_is_persisted_for_diagnosis(db, spec_task):
    # DEV-478's lesson: a failure recorded without its text is a failure nobody
    # can act on. I could not diagnose run 6 of DEV-102 at all.
    spec, task, spec_dir = spec_task
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent",
                               side_effect=["first bad", "second bad", "third bad"]):
            d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                     "implementer", [], None)
    saved = sorted(spec_dir.glob("manifest_failed_response_attempt*.txt"))
    assert len(saved) == 3, [p.name for p in saved]
    bodies = "\n".join(p.read_text() for p in saved)
    for text in ("first bad", "second bad", "third bad"):
        assert text in bodies
    assert "# parse error:" in saved[0].read_text()


def test_persisted_responses_do_not_collide_across_parse_attempts(db, spec_task):
    spec, task, spec_dir = spec_task
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent", side_effect=["a", "b", "c"]):
            d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                     "implementer", [], None)
    names = {p.name for p in spec_dir.glob("manifest_failed_response_attempt*.txt")}
    assert len(names) == 3


def test_an_unwritable_spec_dir_does_not_break_the_run(db, spec_task):
    # Losing the evidence is bad; losing the run over losing the evidence is
    # worse. The persist is best-effort.
    spec, task, spec_dir = spec_task
    with mock.patch.object(executor, "MANIFEST_PARSE_RETRIES", 1):
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("read-only")):
            with mock.patch.object(d, "call_agent", side_effect=["bad", "bad"]):
                res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                               "implementer", [], None)
    assert isinstance(res, ParseError)
