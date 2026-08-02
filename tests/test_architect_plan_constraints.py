"""DEV-107 — the approved plan's decisions must reach the architect.

The architect was prompted from raw spec.md + rejection notes only, so it
could re-derive a language/toolchain from an ambiguous spec that contradicted
the plan the operator already approved: spec_d448e279's plan said
`language: javascript` with a "no TypeScript" clarification, the spec's own
text mentioned "TypeScript (or plain ES modules)", and the architect designed
in TypeScript twice until the spec failed at design. These tests pin the
plumbing: the binding plan fields render into the architect's user message,
and _run_architect actually passes the plan through.
"""
import textwrap

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import build_architect_message
from coding_model_autonomous.models import SpecStatus, TaskStatus

PLAN = textwrap.dedent("""\
    title: "Centipede"
    language: javascript
    target_runtime: "Node 20+"
    clarifications:
      - "No TypeScript — plain ES modules only."
    test_strategy:
      framework: node_test
      required: true
    constraints:
      dependencies_allowed: false
""")

SPEC = "# Centipede\n\nBuild it in TypeScript (or plain ES modules).\n"


def _user_message(messages):
    assert messages[-1]["role"] == "user"
    return messages[-1]["content"]


def test_plan_constraints_render_into_architect_message():
    body = _user_message(build_architect_message(SPEC, plan_yaml=PLAN))
    constraints, _, spec_part = body.partition("## Specification")
    assert "## Approved plan — binding constraints" in constraints
    assert "- Language: javascript" in constraints
    assert "- Target runtime: Node 20+" in constraints
    assert "- Test framework: node_test" in constraints
    assert "- External dependencies allowed: no" in constraints
    assert "No TypeScript — plain ES modules only." in constraints
    assert SPEC.strip() in spec_part, "spec must still follow the constraints"


def test_rejection_notes_still_lead_the_message():
    body = _user_message(build_architect_message(
        SPEC, rejection_notes="design used TypeScript", plan_yaml=PLAN))
    assert body.index("prior design was rejected") \
        < body.index("## Approved plan — binding constraints") \
        < body.index("## Specification")


def test_no_plan_yields_the_pre_dev107_message():
    with_none = _user_message(build_architect_message(SPEC))
    assert "binding constraints" not in with_none
    assert with_none == _user_message(
        build_architect_message(SPEC, plan_yaml=None))


@pytest.mark.parametrize("bad_yaml", [
    "::: not yaml {{{", "- just\n- a\n- list", "title: only-a-title\n"])
def test_unusable_plan_degrades_to_no_constraints(bad_yaml):
    """Unparseable YAML, a non-mapping, or a plan with none of the binding
    fields must not crash and must not emit an empty constraints header."""
    body = _user_message(build_architect_message(SPEC, plan_yaml=bad_yaml))
    assert "binding constraints" not in body


def test_run_architect_passes_the_plan(tmp_path, monkeypatch):
    """_run_architect reads plan.yaml from the spec dir and threads it into
    build_architect_message — the call-site half of DEV-107."""
    db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    try:
        spec = db.create_spec(title="demo", source_md_path="spec.md",
                              status=SpecStatus.EXECUTING)
        task = db.create_task(spec_id=spec.id, agent="architect",
                              role="architect", title="design demo")
        task = db.get_task(task.id)
        spec_dir = db.workspace_root / spec.id
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text(SPEC)
        (spec_dir / "plan.yaml").write_text(PLAN)

        seen = {}

        class _Stop(Exception):
            pass

        def capture(spec_md, rejection_notes=None, plan_yaml=None):
            seen["plan_yaml"] = plan_yaml
            raise _Stop  # skip the agent call — the handoff is the test

        monkeypatch.setattr(d, "build_architect_message", capture)
        with pytest.raises(_Stop):
            d._run_architect(db, spec, task, spec_dir)

        assert seen["plan_yaml"] == PLAN + "\n" or seen["plan_yaml"] == PLAN
    finally:
        db.close_all()
