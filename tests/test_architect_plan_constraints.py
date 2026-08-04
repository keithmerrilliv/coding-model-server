"""DEV-107 — the approved plan's decisions must reach the architect.

The architect was prompted from raw spec.md + rejection notes only, so it
could re-derive a language/toolchain from an ambiguous spec that contradicted
the plan the operator already approved: spec_d448e279's plan said
`language: javascript` with a "no TypeScript" clarification, the spec's own
text mentioned "TypeScript (or plain ES modules)", and the architect designed
in TypeScript twice until the spec failed at design. Rejection notes alone did
not hold — only rewriting spec.md did. These tests pin the plumbing: the
binding plan fields render into the architect's user message as orders that
outrank the spec, and _run_architect actually passes the plan through.
"""
import textwrap
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ArchitectResult, build_architect_message
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
    phases:
      - name: design
        role: architect
""")

SPEC = "# Centipede\n\nBuild it in TypeScript (or plain ES modules).\n"


def _user_message(messages):
    assert messages[-1]["role"] == "user"
    return messages[-1]["content"]


# ── rendering ───────────────────────────────────────────────────────────────

def test_plan_constraints_render_into_architect_message():
    body = _user_message(build_architect_message(SPEC, plan_yaml=PLAN))
    constraints, _, spec_part = body.partition("## Specification")
    assert "## Approved plan — binding constraints" in constraints
    assert "**Language: javascript.**" in constraints
    assert "**Target runtime: Node 20+.**" in constraints
    assert "**Test framework: node_test.**" in constraints
    assert "**External dependencies: NOT permitted.**" in constraints
    assert "No TypeScript — plain ES modules only." in constraints
    assert SPEC.strip() in spec_part, "spec must still follow the constraints"


def test_plan_is_stated_as_outranking_the_spec():
    """The failure mode was the architect treating an ambiguous spec as the
    authority. The prompt has to say, in words, which one wins — and say it
    again after the spec, so the ambiguity is not the last thing read."""
    body = _user_message(build_architect_message(
        SPEC, plan_yaml="language: javascript\n"))
    assert "the plan WINS" in body
    # The restatement grew a second sentence for DEV-490 (the plan's approved
    # acceptance_criteria supersede the spec's). Recency is what this test
    # protects, so assert the original sentence still follows the spec AND is
    # still in the closing block — not that it is the literal final character.
    tail = body.rstrip()[body.rstrip().index(SPEC.strip()) + len(SPEC.strip()):]
    assert (
        "Honor the approved plan's binding constraints above — where the "
        "specification is ambiguous or suggests an alternative, the plan "
        "is the answer."
    ) in tail, "the constraint is restated after the spec for recency"
    assert tail.rstrip().endswith("(DEV-490)."), (
        "the plan-precedence block must remain the last thing the architect "
        "reads before it starts writing")


def test_dependencies_permitted_renders_the_positive_form():
    body = _user_message(build_architect_message(
        SPEC, plan_yaml="language: swift\nconstraints:\n"
                        "  dependencies_allowed: true\n"))
    assert "**External dependencies: permitted.**" in body
    assert "NOT permitted" not in body


def test_partial_plans_render_only_what_is_present():
    body = _user_message(build_architect_message(
        SPEC, plan_yaml="language: swift\n"))
    assert "**Language: swift.**" in body
    assert "Test framework" not in body
    assert "External dependencies" not in body
    assert "Operator clarifications" not in body


def test_constraint_notes_are_carried_through():
    body = _user_message(build_architect_message(
        SPEC, plan_yaml="language: swift\nconstraints:\n"
                        "  notes: no network access at test time\n"))
    assert "**Other constraints:** no network access at test time" in body


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
    "::: not yaml {{{", "- just\n- a\n- list", "title: only-a-title\n", ""])
def test_unusable_plan_degrades_to_no_constraints(bad_yaml):
    """Unparseable YAML, a non-mapping, or a plan with none of the binding
    fields must not crash, must not emit an empty constraints header, and must
    not leave the trailing restatement dangling with nothing to refer to."""
    body = _user_message(build_architect_message(SPEC, plan_yaml=bad_yaml))
    assert "binding constraints" not in body
    assert "Honor the approved plan" not in body
    assert body == _user_message(build_architect_message(SPEC))


def test_malformed_plan_sections_do_not_raise():
    """Planner output is LLM-generated: `test_strategy: pytest` (a scalar, not
    a mapping) is plausible drift and must not take the architect run down."""
    body = _user_message(build_architect_message(
        SPEC,
        plan_yaml=textwrap.dedent("""\
            language: python
            test_strategy: pytest
            constraints:
              - none
            clarifications: be careful
        """),
    ))
    assert "**Language: python.**" in body
    assert "Test framework" not in body
    assert "External dependencies" not in body
    assert "Operator clarifications" not in body


def test_clarifications_alone_still_produce_a_block():
    """A plan carrying only operator clarifications has binding content even
    though none of the scalar decision fields are set."""
    body = _user_message(build_architect_message(
        SPEC, plan_yaml='clarifications:\n  - "No TypeScript."\n'))
    assert "## Approved plan — binding constraints" in body
    assert "No TypeScript." in body


# ── plan loading ────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _architect_spec(db, plan_yaml=PLAN):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    if plan_yaml is not None:
        db.update_spec_status(spec.id, SpecStatus.EXECUTING,
                              normalized_yaml=plan_yaml)
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text(SPEC)
    task = db.create_task(spec_id=spec.id, agent="architect",
                          role="architect", title="design demo")
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def test_load_plan_tolerates_missing_and_broken_yaml(db):
    spec, _task, _dir = _architect_spec(db, plan_yaml=None)
    assert d._load_plan(spec) == {}

    db.update_spec_status(spec.id, SpecStatus.EXECUTING,
                          normalized_yaml="language: [unclosed\n")
    assert d._load_plan(db.get_spec(spec.id)) == {}

    db.update_spec_status(spec.id, SpecStatus.EXECUTING,
                          normalized_yaml="- just\n- a list\n")
    assert d._load_plan(db.get_spec(spec.id)) == {}, \
        "a non-mapping plan is not a plan"


def test_load_plan_returns_the_parsed_plan(db):
    spec, _task, _dir = _architect_spec(db)
    plan = d._load_plan(spec)
    assert plan["language"] == "javascript"
    assert plan["test_strategy"]["framework"] == "node_test"


# ── end to end through the daemon ───────────────────────────────────────────

def test_run_architect_reads_plan_yaml_from_the_spec_dir(db):
    """_run_architect prefers plan.yaml on disk and threads it into
    build_architect_message — the call-site half of DEV-107."""
    spec, task, spec_dir = _architect_spec(db, plan_yaml=None)
    (spec_dir / "plan.yaml").write_text(PLAN)

    seen = {}

    class _Stop(Exception):
        pass

    def capture(spec_md, rejection_notes=None, plan_yaml=None):
        seen["plan_yaml"] = plan_yaml
        raise _Stop  # skip the agent call — the handoff is the test

    with mock.patch.object(d, "build_architect_message", capture):
        with pytest.raises(_Stop):
            d._run_architect(db, spec, task, spec_dir)

    assert seen["plan_yaml"].strip() == PLAN.strip()


def test_run_architect_falls_back_to_the_db_copy(db):
    """No plan.yaml on disk: the DB's normalized_yaml still reaches the
    architect rather than the spec running unconstrained."""
    spec, task, spec_dir = _architect_spec(db)
    assert not (spec_dir / "plan.yaml").exists()

    seen = {}

    class _Stop(Exception):
        pass

    def capture(spec_md, rejection_notes=None, plan_yaml=None):
        seen["plan_yaml"] = plan_yaml
        raise _Stop

    with mock.patch.object(d, "build_architect_message", capture):
        with pytest.raises(_Stop):
            d._run_architect(db, spec, task, spec_dir)

    assert seen["plan_yaml"].strip() == PLAN.strip()


def test_run_architect_sends_the_plan_constraints_to_the_agent(db):
    """The acceptance criterion: plan says javascript, spec waves at
    TypeScript, and the architect is told javascript — on the first attempt,
    with no rejection round and no spec rewrite."""
    spec, task, spec_dir = _architect_spec(db)
    (spec_dir / "plan.yaml").write_text(PLAN)
    ares = ArchitectResult(design_md="# design", raw="x",
                           complexity={"tier": "low",
                                       "recommended_agent": "fast_implementer"})
    with mock.patch.object(d, "call_agent", return_value="raw") as call, \
            mock.patch.object(d, "parse_architect_response", return_value=ares), \
            mock.patch.object(executor, "parse_design_review",
                              return_value=("PASS", "")):
        d._run_architect(db, spec, task, spec_dir)

    # call_args_list[0] — the design-review pass calls the same mock afterwards.
    role, messages = call.call_args_list[0].args[0], call.call_args_list[0].args[1]
    assert role == "architect"
    sent = _user_message(messages)
    assert "**Language: javascript.**" in sent
    assert "No TypeScript — plain ES modules only." in sent
    assert "node_test" in sent
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW, \
        "the architect run should reach the design gate, not bail early"
    assert db.get_task(task.id).retry_count == 0, "no rejection round needed"
