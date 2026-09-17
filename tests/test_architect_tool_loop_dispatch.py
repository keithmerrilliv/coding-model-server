"""DEV-714 wiring: the architect's tool loop inside the real dispatch.

The module's own tests (test_architect_tools.py) cover the protocol. These
cover the thing that has burned me repeatedly — shipping a capability whose
prompt half and mechanism half were never verified together, so the guard was
armed for a case the model never produced (DEV-715, twice).
"""
import textwrap
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind, SpecStatus

PLAN = textwrap.dedent("""\
    title: "demo"
    language: swift
    test_strategy:
      framework: xcodebuild_test
      required: true
      repo: electric-sheep
      base_ref: main
    phases:
      - name: design
        role: architect
        outputs: [design.md]
      - name: implement
        role: implementer
        outputs: [ElectricSheep/Bridge.swift]
""")
PLAN_NO_REPO = PLAN.replace("  repo: electric-sheep\n", "")
# textwrap.dedent already stripped the literal's own indent; asserting the
# substitution happened is the difference between testing the no-repo path and
# testing the with-repo path twice.
assert "repo:" not in PLAN_NO_REPO and "repo: electric-sheep" in PLAN
SPEC = "# Demo\n\nModify `ElectricSheep/Bridge.swift` so it spawns once.\n"
DESIGN = ("<<<DESIGN>>>\n# Architecture: Demo\n\n## Overview\nA cursor.\n"
          "<<<END>>>")
BRIDGE = "class Bridge {\n  func update() { spawnWaveChain() }\n}"
WAVES = "extension Bridge {\n  func spawnWaveChain(count: Int, seed: UInt64) {}\n}"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    def make(plan=PLAN):
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING,
                              normalized_yaml=plan)
        spec_dir = db.spec_dir(spec.id)
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text(SPEC)
        (spec_dir / "plan.yaml").write_text(plan)
        task = db.create_task(spec_id=spec.id, agent="dense_architect",
                             role="architect", title="design")
        return db.get_spec(spec.id), db.get_task(task.id), spec_dir
    return make


REPO = {"ElectricSheep/Bridge.swift": BRIDGE,
        "ElectricSheep/Bridge+Waves.swift": WAVES}


def _fetch(repo, paths, base_ref="HEAD", **kw):
    return ([(p, REPO[p]) for p in paths if p in REPO],
            [f"{p}: not found" for p in paths if p not in REPO])


class _Agent:
    """A scripted architect. Records every ARCHITECT prompt it was sent.

    Downstream roles (the design review that runs on the same dispatch) share
    `call_agent`, so they are answered separately — counting them as architect
    calls would make every "exactly one call" assertion meaningless.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, role, messages, meta=None, **kw):
        if role != "architect":
            return "<<<DESIGN_REVIEW>>>\nVERDICT: PASS\n<<<END_DESIGN_REVIEW>>>"
        self.prompts.append(messages)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _run(db, spec, task, spec_dir, agent, tools=True):
    with mock.patch.object(d, "ARCHITECT_TOOLS", tools), \
            mock.patch.object(d.test_runner, "fetch_repo_files", _fetch), \
            mock.patch.object(d, "call_agent", agent):
        d._run_architect(db, spec, task, spec_dir)


def _events(db, spec_id, kind, role="architect"):
    """Oldest-first, so [-1] is the latest. Filtered to the architect's own
    rows: the design review that follows writes AGENT_RAN too."""
    rows = reversed(db.list_events_by_kind(spec_id=spec_id, kind=kind))
    return [e for e in rows
            if role is None or e.payload.get("role") in (None, role)]


# ── the prompt half ─────────────────────────────────────────────────────────

def test_the_architect_is_told_the_tool_exists(db, spec_task):
    spec, task, spec_dir = spec_task()
    agent = _Agent(DESIGN)
    _run(db, spec, task, spec_dir, agent)
    prompt = agent.prompts[0][1]["content"]
    assert "<<<READ_FILE>>>" in prompt
    assert "do not ask to examine anything" not in prompt


def test_with_tools_off_the_old_instruction_stands(db, spec_task):
    # Both halves must move together: a prompt that offers a tool the loop
    # will not answer is worse than no tool at all.
    spec, task, spec_dir = spec_task()
    agent = _Agent(DESIGN)
    _run(db, spec, task, spec_dir, agent, tools=False)
    prompt = agent.prompts[0][1]["content"]
    assert "<<<READ_FILE>>>" not in prompt
    assert "do not ask to examine anything" in prompt


def test_a_spec_with_no_repository_is_not_offered_a_tool(db, spec_task):
    spec, task, spec_dir = spec_task(PLAN_NO_REPO)
    agent = _Agent(DESIGN)
    _run(db, spec, task, spec_dir, agent)
    assert "<<<READ_FILE>>>" not in agent.prompts[0][1]["content"]


# ── the loop ────────────────────────────────────────────────────────────────

def test_a_design_with_no_request_costs_exactly_one_call(db, spec_task):
    spec, task, spec_dir = spec_task()
    agent = _Agent(DESIGN)
    _run(db, spec, task, spec_dir, agent)
    assert len(agent.prompts) == 1
    ran = _events(db, spec.id, EventKind.AGENT_RAN)[-1]
    assert ran.payload["tool_rounds"] == 0


def test_a_request_is_answered_and_the_design_follows(db, spec_task):
    """The DEV-698 case: the file defining the called symbol was never served,
    so the architect reads it instead of inventing the signature."""
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<READ_FILE>>>ElectricSheep/Bridge+Waves.swift", DESIGN)
    _run(db, spec, task, spec_dir, agent)

    assert len(agent.prompts) == 2, "the request must be answered and re-asked"
    second = agent.prompts[1]
    assert second[-2]["role"] == "assistant"
    answer = second[-1]["content"]
    assert "spawnWaveChain(count: Int, seed: UInt64)" in answer
    assert "TOOL RESULTS (round 1/" in answer
    # The design still lands.
    assert (spec_dir / "design.md").read_text().startswith("# Architecture")


def test_what_it_fetched_is_recorded(db, spec_task):
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<READ_FILE>>>ElectricSheep/Bridge+Waves.swift", DESIGN)
    _run(db, spec, task, spec_dir, agent)

    ran = _events(db, spec.id, EventKind.AGENT_RAN)[-1]
    assert ran.payload["tool_rounds"] == 1
    assert ran.payload["tool_calls"] == [
        "READ_FILE ElectricSheep/Bridge+Waves.swift"]
    assert ran.payload["tool_chars"] == len(WAVES)

    ctx = [e for e in _events(db, spec.id, EventKind.CONTEXT_ASSEMBLED,
                              role=None)
           if e.payload.get("trigger") == "architect_tools"]
    assert len(ctx) == 1, "the architect's own reads need their own ledger row"
    assert ctx[0].payload["repo"] == "electric-sheep"
    assert ctx[0].payload["base_ref"] == "main"


def test_a_model_that_only_ever_asks_terminates(db, spec_task):
    # Otherwise one confused model parks a worker forever.
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<READ_FILE>>>ElectricSheep/Bridge.swift")
    _run(db, spec, task, spec_dir, agent)
    rounds = d.architect_tools.DEFAULT_MAX_ROUNDS
    assert len(agent.prompts) == (rounds + 1) * (
        d.executor.ARCHITECT_PARSE_RETRIES + 1)
    assert "budget is now SPENT" in agent.prompts[rounds][-1]["content"]


def test_an_unanswerable_tool_does_not_loop(db, spec_task):
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<GLOB>>>ElectricSheep/**/*.swift", DESIGN)
    _run(db, spec, task, spec_dir, agent)
    answer = agent.prompts[1][-1]["content"]
    assert "UNAVAILABLE" in answer and "READ_FILE" in answer


def test_markers_never_reach_the_design_on_disk(db, spec_task):
    # The testability checker reads backticked spans; a surviving marker would
    # be scored as design content.
    spec, task, spec_dir = spec_task()
    agent = _Agent(DESIGN + "\n<<<READ_FILE>>>ElectricSheep/Bridge.swift")
    _run(db, spec, task, spec_dir, agent)
    written = (spec_dir / "design.md").read_text()
    assert "READ_FILE" not in written
    assert "A cursor." in written


def test_a_design_that_mentions_a_write_marker_is_not_a_request(db, spec_task):
    """A design describing the implementer's protocol is an answer, not a
    request; feeding it back would spend the attempt on nothing."""
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<DESIGN>>>\n# Architecture: Demo\n\nThe implementer "
                   "emits <<<WRITE_FILE>>>Bridge.swift.\n<<<END>>>")
    _run(db, spec, task, spec_dir, agent)
    assert len(agent.prompts) == 1


def test_a_runner_outage_mid_loop_does_not_fail_the_design(db, spec_task):
    """The architect still holds its served context; a read that fails is a
    message, not the end of the attempt."""
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<READ_FILE>>>ElectricSheep/Bridge+Waves.swift", DESIGN)
    calls = {"n": 0}

    def flaky(repo, paths, base_ref="HEAD", **kw):
        calls["n"] += 1
        if calls["n"] > 1:                      # the context fetch succeeds
            raise ConnectionResetError("ECONNRESET")
        return _fetch(repo, paths, base_ref)

    with mock.patch.object(d, "ARCHITECT_TOOLS", True), \
            mock.patch.object(d.test_runner, "fetch_repo_files", flaky), \
            mock.patch.object(d, "call_agent", agent):
        d._run_architect(db, spec, task, spec_dir)

    assert "COULD NOT READ" in agent.prompts[1][-1]["content"]
    assert (spec_dir / "design.md").exists()


def test_tools_off_leaves_a_request_unanswered_and_unparsed(db, spec_task):
    # The honest failure mode when the switch is off: no silent half-state.
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<READ_FILE>>>ElectricSheep/Bridge.swift")
    _run(db, spec, task, spec_dir, agent, tools=False)
    assert len(agent.prompts) == d.executor.ARCHITECT_PARSE_RETRIES + 1


# ── the window ──────────────────────────────────────────────────────────────

def test_the_offer_is_withdrawn_when_the_window_has_no_room(db, spec_task):
    """Tool results land in the window AFTER the allocator sized it. When the
    served context already fills it, offering a read would buy a 413 instead of
    a design — so the offer goes away with it."""
    spec, task, spec_dir = spec_task()
    agent = _Agent(DESIGN)
    real = d._prompt_budget

    def cramped(*a, **kw):
        alloc = real(*a, **kw)
        alloc.input_budget_chars = alloc.section_chars + 10
        return alloc

    with mock.patch.object(d, "_prompt_budget", cramped):
        _run(db, spec, task, spec_dir, agent)
    assert "<<<READ_FILE>>>" not in agent.prompts[0][1]["content"]


def test_a_roomy_window_keeps_the_offer(db, spec_task):
    # The negative control: without it the test above passes on a bug that
    # withdraws the tool always.
    spec, task, spec_dir = spec_task()
    agent = _Agent(DESIGN)
    _run(db, spec, task, spec_dir, agent)
    assert "<<<READ_FILE>>>" in agent.prompts[0][1]["content"]


def test_reads_are_capped_by_the_headroom_not_the_constant(db, spec_task):
    spec, task, spec_dir = spec_task()
    agent = _Agent("<<<READ_FILE>>>ElectricSheep/Bridge+Waves.swift", DESIGN)
    real = d._prompt_budget
    cap = d.architect_tools.MIN_USEFUL_CHARS + 1

    def tight(*a, **kw):
        alloc = real(*a, **kw)
        alloc.input_budget_chars = alloc.section_chars + cap
        return alloc

    with mock.patch.object(d, "_prompt_budget", tight):
        _run(db, spec, task, spec_dir, agent)
    ran = _events(db, spec.id, EventKind.AGENT_RAN)[-1]
    assert 0 < ran.payload["tool_chars"] <= cap
