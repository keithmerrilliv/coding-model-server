"""DEV-657 part 1: retrieval is gated on the spec's language, not only the role.

The corpus is Apple documentation. Retrieval was conditioned on the role and
nothing else, so an all-Apple corpus was queried for every opted-in call
whatever the spec was written in.

Measured on the live event store before this was written (last 3,000 AGENT_RAN
rows, the ones carrying DEV-657 part 2's durable outcome):

    language   outcome     n
    python     injected    5     <- every one a false injection
    swift      empty      11
    swift      injected    5
    swift      skipped     1

Every retrieval this gate removes is a confirmed injection of Apple docs into a
Python prompt; no Swift retrieval is lost. That is the shape these tests pin.
"""
import ast
import inspect
import re

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor as ex


# ── the decision ────────────────────────────────────────────────────────────

class TestRetrievalDecision:
    @pytest.fixture(autouse=True)
    def _opted_in(self, monkeypatch):
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES",
                            {"architect", "implementer"})
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_LANGUAGES", {"swift"})

    def test_covered_language_on_an_opted_in_role_retrieves(self):
        assert ex.retrieval_decision("architect", "swift") == (True, "retrieved")

    def test_an_uncovered_language_does_not(self):
        ok, why = ex.retrieval_decision("architect", "python")
        assert (ok, why) == (False, "language_not_covered")

    def test_a_role_that_never_opted_in_still_does_not(self):
        ok, why = ex.retrieval_decision("planner", "swift")
        assert (ok, why) == (False, "role_not_opted_in")

    def test_the_role_check_comes_first(self):
        # A role that never opted in should read as a role decision, not be
        # attributed to the language gate this ticket added.
        assert ex.retrieval_decision("planner", "python")[1] == "role_not_opted_in"

    @pytest.mark.parametrize("lang", [None, "", "   "])
    def test_an_unreadable_language_does_not_retrieve(self, lang):
        # "We could not tell" is not evidence the spec is Swift. DEV-630.
        ok, why = ex.retrieval_decision("architect", lang)
        assert (ok, why) == (False, "language_unknown")

    @pytest.mark.parametrize("lang", ["Swift", "SWIFT", "  swift  "])
    def test_language_matching_ignores_case_and_padding(self, lang):
        assert ex.retrieval_decision("architect", lang)[0] is True

    def test_the_covered_set_is_configuration(self, monkeypatch):
        # The day a Python corpus is indexed, this is the only line that moves.
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_LANGUAGES", {"swift", "python"})
        assert ex.retrieval_decision("architect", "python")[0] is True

    def test_the_default_covers_swift_only(self):
        assert ex._parse_memory_roles("swift") == {"swift"}


# ── what the call actually sends ────────────────────────────────────────────

class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _capture(monkeypatch, rag=None):
    """Record the kwargs call_agent sends to the server."""
    sent = {}
    payload = {"choices": [{"message": {"content": "hi"},
                            "finish_reason": "stop"}]}
    if rag is not None:
        payload["rag"] = rag

    def fake(model, messages, **kw):
        sent.update(kw)
        return _Resp(payload)

    monkeypatch.setattr(ex, "post_chat_completion", fake)
    return sent


class TestCallAgent:
    @pytest.fixture(autouse=True)
    def _opted_in(self, monkeypatch):
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", {"architect"})
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_LANGUAGES", {"swift"})

    def test_a_swift_spec_asks_the_server_to_retrieve(self, monkeypatch):
        sent = _capture(monkeypatch)
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="cancel MLX generation", language="swift")
        assert sent["skip_memory"] is False
        assert sent["memory_query"] == "cancel MLX generation"

    def test_a_python_spec_does_not(self, monkeypatch):
        sent = _capture(monkeypatch)
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="parse the test strategy", language="python")
        assert sent["skip_memory"] is True
        # And the query is not sent either — the server would ignore it, but a
        # payload carrying a query it will not use invites the next reader to
        # believe retrieval happened.
        assert "memory_query" not in sent

    def test_an_absent_language_does_not_retrieve(self, monkeypatch):
        sent = _capture(monkeypatch)
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="q")
        assert sent["skip_memory"] is True


# ── the record ──────────────────────────────────────────────────────────────

class TestTelemetry:
    @pytest.fixture(autouse=True)
    def _opted_in(self, monkeypatch):
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", {"architect"})
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_LANGUAGES", {"swift"})

    def test_the_gate_reason_rides_on_the_servers_record(self, monkeypatch):
        _capture(monkeypatch, rag={"outcome": "injected", "hits": 5})
        meta = {}
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="q", language="swift", meta=meta)
        assert meta["rag"] == {"outcome": "injected", "hits": 5,
                               "gate": "retrieved"}

    def test_a_gated_call_records_why_it_never_ran(self, monkeypatch):
        # "skipped" alone pools the role opt-out, the language gate and an
        # unreadable plan into one value; part 3's A/B cannot tell which arm a
        # call was in without this.
        _capture(monkeypatch, rag={"outcome": "skipped"})
        meta = {}
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="q", language="python", meta=meta)
        assert meta["rag"] == {"outcome": "skipped",
                               "gate": "language_not_covered"}

    def test_an_old_server_still_yields_a_record(self, monkeypatch):
        # No `rag` key from the server. Absence used to mean "server predates
        # part 2"; `not_requested` is a new value that keeps that readable.
        _capture(monkeypatch, rag=None)
        meta = {}
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="q", language="python", meta=meta)
        assert meta["rag"] == {"outcome": "not_requested",
                               "gate": "language_not_covered"}

    def test_an_old_server_on_a_retrieving_call_records_nothing(self, monkeypatch):
        # Here absence must survive: we asked, and this server cannot say what
        # happened. Synthesising a record would invent an outcome.
        _capture(monkeypatch, rag=None)
        meta = {}
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="q", language="swift", meta=meta)
        assert "rag" not in meta

    def test_the_record_reaches_the_event_payload(self, monkeypatch):
        _capture(monkeypatch, rag={"outcome": "skipped"})
        meta = {}
        ex.call_agent("architect", [{"role": "user", "content": "x"}],
                      memory_query="q", language="python", meta=meta)
        assert ex.agent_event_fields(meta)["rag"]["gate"] == "language_not_covered"


# ── the structural guard ────────────────────────────────────────────────────

def test_every_retrieving_call_site_passes_a_language():
    """A call that asks for retrieval must say what language it is for.

    This is the failure this ticket exists to prevent, and the next call site
    is where it comes back: `memory_query=` without `language=` retrieves
    ungated again, and nothing downstream would look wrong — the corpus would
    simply start answering Python questions. Checked structurally because a
    behavioural test only covers the sites that already exist.
    """
    tree = ast.parse(inspect.getsource(d))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "call_agent":
            continue
        kw = {k.arg for k in node.keywords}
        if "memory_query" in kw and "language" not in kw:
            offenders.append(node.lineno)
    assert not offenders, (
        "call_agent sites pass memory_query without language "
        f"(ungated retrieval) at orchestrator_daemon.py lines {offenders}")


def test_the_guard_can_fail():
    """The negative control: without it the test above passes on a tree with
    no call_agent in it at all."""
    tree = ast.parse("call_agent('architect', m, memory_query='q')\n")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert {k.arg for k in call.keywords} == {"memory_query"}


def test_the_language_comes_from_the_plan():
    src = inspect.getsource(d._spec_language)
    assert "_load_plan" in src
    assert re.search(r'get\(\s*["\']language["\']\s*\)', src)


# ── reading the language off a real spec ────────────────────────────────────

class TestSpecLanguage:
    @pytest.fixture
    def db(self, tmp_path):
        from coding_model_autonomous.db import Database
        database = Database(db_path=tmp_path / "t.sqlite",
                            workspace_root=tmp_path / "ws")
        yield database
        database.close_all()

    def _spec(self, db, plan):
        from coding_model_autonomous.models import SpecStatus
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING, normalized_yaml=plan)
        return db.get_spec(spec.id)

    def test_reads_the_plans_language(self, db):
        spec = self._spec(db, "title: t\nlanguage: swift\n")
        assert d._spec_language(spec) == "swift"

    def test_python_is_read_as_python(self, db):
        spec = self._spec(db, "title: t\nlanguage: python\n")
        assert d._spec_language(spec) == "python"

    def test_a_plan_without_a_language_is_none(self, db):
        # None, not a default: the gate must treat this as "not known to be
        # covered" rather than silently choosing an answer.
        spec = self._spec(db, "title: t\n")
        assert d._spec_language(spec) is None

    def test_an_unreadable_plan_is_none(self, db):
        spec = self._spec(db, "this: is: not: yaml:\n")
        assert d._spec_language(spec) is None

    def test_a_blank_language_is_none(self, db):
        spec = self._spec(db, 'title: t\nlanguage: "   "\n')
        assert d._spec_language(spec) is None
