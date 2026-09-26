"""DEV-760: a rejection retry gets more room, and a budget spent thinking is
neither re-rolled unchanged nor lost from the record.

Run 46 (spec_c6f4902f): the architect's first pass wrote a complete design in
4,879 completion tokens. Its rejection retry spent 10,000 four times: three
reasoning-only 502s, re-sent unchanged by the transport ladder, then a
`finish_reason: length` cut off forty words into the correct design. None of
the three 502s reached the event stream.
"""
import textwrap
from unittest import mock

import pytest
import requests

import coding_model_autonomous._http as h
import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus
from coding_model_autonomous.outcome import FailureClass, classify_exception
from coding_model_server.streaming import build_completion_response

PLAN = textwrap.dedent("""\
    title: "demo"
    language: swift
    test_strategy:
      framework: xcodebuild_test
      required: true
    phases:
      - name: design
        role: architect
        outputs: [design.md]
""")
DESIGN = "<<<DESIGN>>>\n# Architecture: Demo\n\n## Overview\nA cursor.\n<<<END>>>"
REASONING_ONLY = ('{"detail":"model produced no visible content after 10000 '
                  'completion tokens (reasoning-only response; request_id=r1)"}')


# ── the budget ───────────────────────────────────────────────────────────────

def test_a_retry_gets_more_room_than_a_first_pass():
    assert executor.architect_max_tokens(False) == executor.ARCHITECT_MAX_TOKENS
    assert executor.architect_max_tokens(True) == executor.ARCHITECT_RETRY_MAX_TOKENS
    assert executor.ARCHITECT_RETRY_MAX_TOKENS > executor.ARCHITECT_MAX_TOKENS


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _architect_budgets(db, *, retry):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING, normalized_yaml=PLAN)
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Demo\n\nA cursor.\n")
    (spec_dir / "plan.yaml").write_text(PLAN)
    task = db.create_task(spec_id=spec.id, agent="dense_architect",
                          role="architect", title="design")
    if retry:
        db.increment_task_retry(task.id)
    budgets = []

    def agent(role, messages, meta=None, max_tokens=None, **kw):
        if role == "architect":
            budgets.append(max_tokens)
            return DESIGN
        return "<<<DESIGN_REVIEW>>>\nVERDICT: PASS\n<<<END_DESIGN_REVIEW>>>"

    with mock.patch.object(d, "call_agent", agent), \
            mock.patch.object(d, "_latest_architect_feedback",
                              return_value="testability check: 1 finding"), \
            mock.patch.object(d.test_runner, "fetch_repo_files",
                              return_value=([], [])):
        d._run_architect(db, db.get_spec(spec.id), db.get_task(task.id), spec_dir)
    return budgets


def test_the_architect_sends_the_retry_budget_when_answering_a_rejection(db):
    assert _architect_budgets(db, retry=True) == [executor.ARCHITECT_RETRY_MAX_TOKENS]


def test_a_first_pass_keeps_the_ordinary_budget(db):
    assert _architect_budgets(db, retry=False) == [executor.ARCHITECT_MAX_TOKENS]


# ── the transport ladder ─────────────────────────────────────────────────────

def _resp(status, text):
    r = mock.Mock()
    r.status_code, r.text, r.headers = status, text, {}
    return r


def _posts_for(response):
    posts = []

    def fake_post(*a, **kw):
        posts.append(1)
        return response

    with mock.patch.object(h._SESSION, "post", side_effect=fake_post), \
            mock.patch.object(h.time, "sleep"):
        h.post_chat_completion("dense_architect", [{"role": "user", "content": "x"}],
                               timeout=60, retry_5xx=True)
    return len(posts)


def test_a_reasoning_only_502_is_sent_once():
    assert _posts_for(_resp(502, REASONING_ONLY)) == 1


def test_a_transient_502_still_gets_the_whole_ladder():
    """The negative control: a crashed child is transient and must keep its
    backoff, or a model-swap blip would fail a spec."""
    upstream = _resp(502, '{"detail":"upstream inference error (request_id=r2)"}')
    assert _posts_for(upstream) == len(h._BACKOFFS) + 1


# ── the record ───────────────────────────────────────────────────────────────

def _http_error(status, text):
    resp = requests.Response()
    resp.status_code = status
    resp._content = text.encode()
    return requests.HTTPError(f"{status} Server Error", response=resp)


def test_a_reasoning_only_502_is_recorded_as_an_empty_completion_with_its_spend():
    f = classify_exception(_http_error(502, REASONING_ONLY), role="architect")
    assert f.cls is FailureClass.EMPTY_COMPLETION
    assert "10000 completion tokens" in f.detail
    assert f.extra.get("reasoning_only") is True


def test_any_other_502_stays_an_http_refusal():
    f = classify_exception(_http_error(502, '{"detail":"upstream inference error"}'),
                           role="architect")
    assert f.cls is FailureClass.HTTP_REFUSAL


def test_the_reasoning_split_reaches_the_response_and_the_event():
    usage = {"prompt_tokens": 10, "completion_tokens": 900, "total_tokens": 910,
             "reasoning_chars": 3200, "visible_chars": 450}
    out = build_completion_response("m", "answer", usage)["usage"]
    assert out["reasoning_chars"] == 3200 and out["visible_chars"] == 450
    fields = executor.agent_event_fields({"agent": "a", "reasoning_chars": 3200,
                                          "visible_chars": 450})
    assert fields["reasoning_chars"] == 3200 and fields["visible_chars"] == 450


def test_an_unmeasured_split_is_absent_not_zero():
    out = build_completion_response(
        "m", "x", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})["usage"]
    assert "reasoning_chars" not in out and "visible_chars" not in out
