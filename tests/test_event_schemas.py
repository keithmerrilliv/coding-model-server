"""DEV-669: the three taxonomy events have a fixed, documented payload.

Each writer's payload must carry every required key and no key the schema
does not list — so a field cannot be added or dropped without the contract
in models.EVENT_PAYLOAD_SCHEMAS (and section 11 of docs/PIPELINE.md)
changing with it.
"""
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import context as c
from coding_model_autonomous import outcome as o
from coding_model_autonomous import retry_policy as rp
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import (
    EVENT_PAYLOAD_SCHEMAS, EventKind, check_event_payload,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="b")
    return spec, task


def _latest(db, spec_id, kind):
    return o._payload(db.list_events_by_kind(spec_id=spec_id, kind=kind)[0])


class TestSchemasAreDocumented:
    def test_every_taxonomy_kind_has_a_schema(self):
        assert set(EVENT_PAYLOAD_SCHEMAS) == {
            EventKind.FAILURE_CLASSIFIED, EventKind.ATTEMPT_PLANNED, EventKind.CONTEXT_ASSEMBLED}
        for schema in EVENT_PAYLOAD_SCHEMAS.values():
            for key, meaning in {**schema["required"], **schema["optional"]}.items():
                assert key and meaning, f"{key!r} needs a meaning"

    def test_the_docs_list_every_kind(self):
        text = (d.Path(__file__).resolve().parent.parent / "docs" / "PIPELINE.md").read_text()
        for kind in EVENT_PAYLOAD_SCHEMAS:
            assert f"`{kind.value}`" in text

    def test_check_names_the_problem(self):
        problems = check_event_payload(EventKind.ATTEMPT_PLANNED, {"role": "x", "bogus": 1})
        assert "undocumented key 'bogus'" in problems
        assert "missing required key 'retry'" in problems
        assert check_event_payload(EventKind.AGENT_RAN, {"anything": 1}) == []


class TestWritersFit:
    def test_a_verdict(self, db, spec_task):
        spec, task = spec_task
        o._record(db, spec, task, o.Failure(
            o.FailureClass.BUILD_FAILURE, "implementer", "build_check",
            "x.py:1:1: error: boom", feedback="x.py:1:1: error: boom\n", phase="build_check"),
            "charge", 0, detail="why")
        assert check_event_payload(EventKind.FAILURE_CLASSIFIED,
                                   _latest(db, spec.id, EventKind.FAILURE_CLASSIFIED)) == []

    def test_a_no_verdict_with_extras(self, db, spec_task):
        spec, task = spec_task
        o._record(db, spec, task, o.Failure(
            o.FailureClass.HTTP_REFUSAL, "implementer", "model_call", "413",
            rotate=True, exc_type="HTTPError", extra={"status": 413, "agent": "implementer"}),
            "rotate", 1)
        assert check_event_payload(EventKind.FAILURE_CLASSIFIED,
                                   _latest(db, spec.id, EventKind.FAILURE_CLASSIFIED)) == []

    def test_every_extra_key_the_daemon_attaches_is_documented(self):
        """The optional set is the union of every Failure(extra={...}) key in
        the source tree; a new one must be added to the schema."""
        import re
        src = d.Path(d.__file__).read_text() + d.Path(o.__file__).read_text()
        keys = set()
        for block in re.findall(r"extra=\{([^}]*)\}", src):
            keys |= set(re.findall(r'"([a-z_]+)"\s*:', block))
        documented = set(EVENT_PAYLOAD_SCHEMAS[EventKind.FAILURE_CLASSIFIED]["optional"])
        assert keys <= documented, keys - documented

    def test_an_attempt_plan(self, db, spec_task):
        spec, task = spec_task
        plan = rp.plan_attempt(db, spec.id, task, role="implementer", agent="implementer",
                               feedback=None, prompt_inputs=("d",), strategy={"repo": "r"})
        rp.record_attempt_plan(db, spec.id, task, plan)
        assert check_event_payload(EventKind.ATTEMPT_PLANNED,
                                   _latest(db, spec.id, EventKind.ATTEMPT_PLANNED)) == []

    def test_a_context_fetch(self, tmp_path):
        plan = {"test_strategy": {"repo": "r", "base_ref": "HEAD"},
                "phases": [{"name": "implement", "role": "implementer", "outputs": ["src/a.py"]}]}
        ctx, fetched = c.assemble(
            spec_id="s", spec_dir=tmp_path, plan=plan, spec_md="", role="implementer",
            fetch=lambda repo, paths, ref: ([("src/a.py", "x")], []))
        assert fetched
        assert check_event_payload(EventKind.CONTEXT_ASSEMBLED,
                                   {"trigger": "implementer", **ctx.summary()}) == []
