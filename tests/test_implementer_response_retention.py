"""DEV-637: the implementer's raw response and the COMPLETE text of every
failed SEARCH block are retained, so an unappliable-edit rotation can be
classified after the fact.

Run 21 spent six rotations on anchors that did not match; the event kept one
line per error, the journal kept the same line, and the retry snapshot kept
no code — the misses were unrecoverable. Now:

  - apply_edits carries a structured EditFailure per error (full SEARCH);
  - the unappliable-edit event records them as ``errors_full``;
  - every attempt writes ``implementer_response.md`` into the workspace,
    which the next attempt's cleanup snapshots into retry_history/retry_<N>/;
  - synthesis never treats that file as merge input.
"""
import json
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.apply_edits import (
    EditBlock,
    apply_search_replace,
    resolve_edits,
)
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult, ParseError
from coding_model_autonomous.models import EventKind, TaskStatus
from coding_model_autonomous.retry_policy import (
    _clean_spec_dir_for_retry,
    _read_retry_attempts,
)

# An 8-line anchor: longer than the 4-line preview _snippet keeps, so a test
# can tell "the complete SEARCH" from "the preview" by content.
LONG_ANCHOR = "\n".join(f"    line_{i} = {i}" for i in range(8))
CURRENT = "def f():\n    return 1\n"
EDIT_WITH_LONG_ANCHOR = (
    "### src/app.py\n"
    "<<<<<<< SEARCH\n"
    f"{LONG_ANCHOR}\n"
    "=======\n"
    "    replaced = True\n"
    ">>>>>>> REPLACE\n"
)


# ── apply_edits: structured failures ─────────────────────────────────────────

def test_not_found_carries_the_whole_failed_block():
    out = apply_search_replace(CURRENT, [EditBlock(search=LONG_ANCHOR, replace="x")])
    assert not out.ok
    assert out.reason == "not_found"
    assert out.failed_index == 1
    assert out.failed_block.search == LONG_ANCHOR
    # The prose diagnostic still previews only the first lines.
    assert "(+4 more line(s))" in out.error


def test_ambiguous_and_empty_reasons():
    amb = apply_search_replace("a\na\n", [EditBlock(search="a", replace="b")])
    assert amb.reason == "ambiguous" and amb.failed_block.search == "a"
    empty = apply_search_replace("a\n", [EditBlock(search="", replace="b")])
    assert empty.reason == "empty_search" and empty.failed_index == 1


def test_resolve_failures_are_parallel_to_errors_with_full_search():
    res = resolve_edits([], EDIT_WITH_LONG_ANCHOR, {"src/app.py": CURRENT})
    assert len(res.errors) == len(res.failures) == 1
    f = res.failures[0]
    assert (f.path, f.block, f.reason) == ("src/app.py", 1, "not_found")
    assert f.search == LONG_ANCHOR
    assert f.detail == res.errors[0]


def test_resolve_records_no_base_and_malformed_failures():
    text = EDIT_WITH_LONG_ANCHOR + "<<<<<<< SEARCH\nstray\n"   # unterminated
    res = resolve_edits([], text, {})                            # no base file
    reasons = {f.reason for f in res.failures}
    assert {"no_base", "malformed"} <= reasons
    no_base = next(f for f in res.failures if f.reason == "no_base")
    assert no_base.path == "src/app.py" and no_base.block == 0
    assert no_base.search == LONG_ANCHOR
    assert len(res.errors) == len(res.failures)


# ── daemon: event payload and the persisted response ─────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _spec_with_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Spec")
    (spec_dir / "design.md").write_text("# Design")
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    db.update_task_status(task.id, TaskStatus.RUNNING)
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _latest_agent_ran(db, spec_id):
    events = db.list_events_by_kind(spec_id=spec_id, kind=EventKind.AGENT_RAN)
    return json.loads(events[0].payload_json)


def _failed_result(raw=EDIT_WITH_LONG_ANCHOR):
    res = resolve_edits([], raw, {"src/app.py": CURRENT})
    return ImplementerResult(
        files=[], raw=raw, apply_errors=res.errors,
        apply_failures=[f.__dict__ for f in res.failures])


def test_generate_implementation_surfaces_structured_failures(db):
    spec, task, spec_dir = _spec_with_task(db)
    with mock.patch.object(executor, "DIFF_BASED_EDITS", True), \
            mock.patch.object(d, "_fetch_existing_files_for_spec",
                              return_value=[("src/app.py", CURRENT)]), \
            mock.patch.object(d, "_fetch_protected_files_for_spec", return_value=[]), \
            mock.patch.object(d, "call_agent", return_value=EDIT_WITH_LONG_ANCHOR):
        res = d._generate_implementation(db, spec, task, spec_dir, "S",
                                         "## Files\nsrc/app.py", "implementer",
                                         [], None)
    assert isinstance(res, ImplementerResult)
    assert res.apply_failures[0]["search"] == LONG_ANCHOR
    assert res.apply_failures[0]["reason"] == "not_found"
    assert "(+4 more line(s))" in res.apply_errors[0]


def test_route_records_errors_full_with_complete_search(db):
    spec, task, spec_dir = _spec_with_task(db)
    result = _failed_result()

    d._route_unappliable_edits(db, spec, task, result.apply_errors,
                               failures=result.apply_failures)

    payload = _latest_agent_ran(db, spec.id)
    assert payload["anomaly"] == "unappliable_edits"
    # The summary line stays one line; the full record carries the anchor.
    assert "\n" not in payload["errors"][0]
    full = payload["errors_full"][0]
    assert full["search"] == LONG_ANCHOR
    assert (full["path"], full["block"], full["reason"]) == ("src/app.py", 1, "not_found")


def test_route_without_failures_keeps_the_old_payload_shape(db):
    spec, task, spec_dir = _spec_with_task(db)
    d._route_unappliable_edits(db, spec, task, ["`src/app.py`: edit block #1: x"])
    assert "errors_full" not in _latest_agent_ran(db, spec.id)


def test_persisted_response_holds_full_search_and_verbatim_raw(db):
    spec, task, spec_dir = _spec_with_task(db)
    result = _failed_result()

    d._persist_implementer_response(spec_dir, task, "deep_implementer", result,
                                    {"prompt_tokens": 7, "truncated": False})

    text = (spec_dir / "implementer_response.md").read_text()
    assert "- agent: deep_implementer" in text
    assert "- prompt_tokens: 7" in text
    assert "SEARCH (complete, 8 line(s)):" in text
    assert LONG_ANCHOR in text
    head, raw = text.split(d._RAW_RESPONSE_DELIMITER + "\n", 1)
    assert raw == EDIT_WITH_LONG_ANCHOR          # byte-for-byte, no fences


def test_parse_error_response_is_persisted_too(db):
    spec, task, spec_dir = _spec_with_task(db)
    d._persist_implementer_response(spec_dir, task, "implementer",
                                    ParseError("No blocks found", "just prose"))
    text = (spec_dir / "implementer_response.md").read_text()
    assert "- result: ParseError" in text
    assert "- parse_error: No blocks found" in text
    assert text.endswith("just prose")


def test_persist_never_raises(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")           # a file where the workspace should be
    task = mock.Mock(retry_count=0)
    d._persist_implementer_response(blocker, task, "x", _failed_result())


def test_run_implementer_writes_the_response_and_cleanup_snapshots_it(db):
    spec, task, spec_dir = _spec_with_task(db)
    with mock.patch.object(d, "_generate_implementation",
                           return_value=_failed_result()):
        d._run_implementer(db, spec, task, spec_dir)

    # Attempt 0 refused → rotated, and the response is on disk at the root.
    assert db.get_task(task.id).status is TaskStatus.PENDING
    live = spec_dir / "implementer_response.md"
    assert live.is_file() and LONG_ANCHOR in live.read_text()

    # The next attempt's cleanup moves it into that attempt's snapshot.
    _clean_spec_dir_for_retry(spec_dir, retry_count=1)
    snap = spec_dir / "retry_history" / "retry_0" / "implementer_response.md"
    assert snap.is_file() and LONG_ANCHOR in snap.read_text()
    assert not live.exists()


def test_synthesis_corpus_ignores_the_retained_response(tmp_path):
    spec_dir = tmp_path / "spec"
    snap = spec_dir / "retry_history" / "retry_0"
    snap.mkdir(parents=True)
    (snap / "implementer_response.md").write_text("raw stuff")
    (snap / "src").mkdir()
    (snap / "src" / "app.py").write_text("print(1)\n")
    (spec_dir / "implementer_response.md").write_text("live raw")
    (spec_dir / "src").mkdir()
    (spec_dir / "src" / "app.py").write_text("print(2)\n")

    attempts = _read_retry_attempts(spec_dir)

    assert [set(a["files"]) for a in attempts] == [{"src/app.py"}, {"src/app.py"}]
