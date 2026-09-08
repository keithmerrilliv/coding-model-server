"""Drive the real daemon state machine against the seam fakes (DEV-634).

A seam test builds a spec in a throwaway Database, scripts the two fakes,
and calls ``drive`` — which ticks the daemon inline (``tick(db)`` with no
scheduler, exactly the path the daemon's own tests use), answers gates the
way an operator would, and stops at a terminal status, an unanswered gate,
a stall or the tick budget. Everything the daemon does between those points
is the production code: prompt building, parsing, routing, retention,
rotation, gate creation, the structural test guard.

The fixture is run 23's self-target spec (DEV-599: the daemon's existing-file
fetch names the role it serves) — a real modify-spec whose plan carries a
``repo`` key, so the existing-file fetch, the protected-path fetch, edit mode
and the pre-gate build check all engage.
"""
from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import (
    ArtifactKind, Database, EventKind, GateStatus, GateType, SpecStatus,
    TaskStatus,
)
from coding_model_autonomous.models import ReviewGate, Spec, Task

from seam_fakes import FakeModelServer, FakeRunner, UnscriptedCall

FIXTURES = Path(__file__).parent / "fixtures"
SPEC_MD = (FIXTURES / "spec.md").read_text()
PLAN_YAML = (FIXTURES / "plan.yaml").read_text()
DESIGN_MD = (FIXTURES / "design.md").read_text()

DAEMON_PATH = "src/coding_model_server/orchestrator_daemon.py"
TEST_PATH = "tests/test_existing_fetch_role_log.py"

# The slice of the daemon the fixture spec modifies, as the runner would serve
# it at base_ref. Small on purpose: the seam tier tests routing, not the edit
# ladder (tests/test_tolerant_edit_apply.py owns that).
DAEMON_STUB = textwrap.dedent('''\
    """Existing-file fetch (fixture stand-in for the daemon)."""
    import logging

    logger = logging.getLogger(__name__)


    def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=()):
        candidates = list(extra_paths)
        if not candidates:
            return []
        files, problems = _read(candidates)
        for problem in problems:
            logger.warning("spec %s: existing-file read problem — %s",
                           spec.id, problem)
        if files:
            logger.info("spec %s: supplied %d existing file(s) to the implementer: %s",
                        spec.id, len(files), ", ".join(p for p, _ in files))
        elif candidates:
            logger.warning("spec %s: %d file(s) marked modify but none could be "
                           "read — implementer is working blind",
                           spec.id, len(candidates))
        return files


    def _read(paths):
        return [], []
''')

DAEMON_STUB_IMPLEMENTED = DAEMON_STUB.replace(
    "def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=()):",
    "def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=(), *, role=\"implementer\"):",
).replace(
    'existing file(s) to the implementer: %s",\n                    spec.id, len(files),',
    'existing file(s) to the %s: %s",\n                    spec.id, len(files), role,',
).replace(
    'read — implementer is working blind",\n                           spec.id, len(candidates))',
    'read — %s is working blind",\n                           spec.id, len(candidates), role)',
)

TEST_FILE = textwrap.dedent('''\
    """DEV-599: the fetch helper names the role it serves."""
    import logging
    from types import SimpleNamespace

    import coding_model_server.orchestrator_daemon as d


    def test_architect_named(monkeypatch, caplog):
        monkeypatch.setattr(d, "_read", lambda paths: ([("a.py", "x")], []))
        with caplog.at_level(logging.INFO):
            d._fetch_existing_files_for_spec(SimpleNamespace(id="s"), "", ("a.py",), role="architect")
        assert "to the architect" in caplog.text
''')

# ── canned role replies ──────────────────────────────────────────────────────

def architect_reply(design_md: str = DESIGN_MD, *, tier: str = "medium",
                    agent: str = "implementer") -> str:
    return (f"<<<DESIGN>>>\n{design_md}\n<<<END>>>\n\n"
            f"<<<COMPLEXITY>>>\ntier: {tier}\nrecommended_agent: {agent}\n"
            f"justification: fixture\n<<<END_COMPLEXITY>>>\n")


def design_review_reply(verdict: str = "PASS", notes: str = "") -> str:
    return f"<<<DESIGN_REVIEW>>>\nVERDICT: {verdict}\n{notes}\n<<<END_DESIGN_REVIEW>>>\n"


def file_blocks(files: dict[str, str]) -> str:
    return "".join(f"<<<FILE: {p}>>>\n{c}\n<<<END_FILE>>>\n\n" for p, c in files.items())


def implementer_reply(files: dict[str, str] | None = None) -> str:
    """Whole-file reply: the implemented daemon slice plus the new test."""
    if files is None:
        files = {DAEMON_PATH: DAEMON_STUB_IMPLEMENTED, TEST_PATH: TEST_FILE}
    return file_blocks(files)


def edit_block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


def implementer_edit_reply(edits: dict[str, list[tuple[str, str]]],
                           new_files: dict[str, str] | None = None) -> str:
    """Edit-mode reply: `### path` + SEARCH/REPLACE blocks, then whole new files."""
    out = []
    for path, blocks in edits.items():
        out.append(f"### {path}\n")
        out.extend(edit_block(s, r) for s, r in blocks)
        out.append("\n")
    if new_files:
        out.append(file_blocks(new_files))
    return "".join(out)


GOOD_EDITS = {DAEMON_PATH: [
    ("def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=()):",
     "def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=(), *, role=\"implementer\"):"),
    ('        logger.info("spec %s: supplied %d existing file(s) to the implementer: %s",\n'
     '                    spec.id, len(files), ", ".join(p for p, _ in files))',
     '        logger.info("spec %s: supplied %d existing file(s) to the %s: %s",\n'
     '                    spec.id, len(files), role, ", ".join(p for p, _ in files))'),
]}

BAD_EDITS = {DAEMON_PATH: [
    ("def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=(), role=None):\n"
     "    candidates = sorted(extra_paths)",
     "def _fetch_existing_files_for_spec(spec, spec_md, extra_paths=(), *, role=\"implementer\"):\n"
     "    candidates = list(extra_paths)"),
]}


def reviewer_reply(verdict: str = "PASS", tests: dict[str, str] | None = None,
                   *, evidence: str = "- AC1 → tests/test_seam.py::test_architect_named",
                   review: str = "Implementation matches the design.") -> str:
    if tests is None:
        tests = {"tests/test_seam.py": TEST_FILE}
    return (file_blocks(tests) +
            f"<<<REVIEW>>>\n## Review\n\n{review}\n\n### Verdict\n{verdict}\n\n"
            f"### Verdict Evidence\n{evidence}\n<<<END_REVIEW>>>\n")


def planner_reply(plan_yaml: str = PLAN_YAML) -> str:
    return f"<<<YAML>>>\n{plan_yaml}\n<<<END>>>\n"


def planner_clarify(*questions: str) -> str:
    body = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    return f"<<<CLARIFY>>>\n{body}\n<<<END>>>\n"


# ── spec construction ────────────────────────────────────────────────────────

def make_spec(db: Database, *, status: SpecStatus = SpecStatus.EXECUTING,
              spec_md: str = SPEC_MD, plan_yaml: str | None = PLAN_YAML,
              design_md: str | None = None,
              title: str = "DEV-599 fetch role log") -> Spec:
    """A spec in *status* with its inputs on disk, ready for the daemon."""
    spec = db.create_spec(title=title, source_md_path="spec.md", status=status)
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text(spec_md)
    if plan_yaml is not None:
        db.update_spec_status(spec.id, status, normalized_yaml=plan_yaml)
        (spec_dir / "plan.yaml").write_text(plan_yaml)
    if design_md is not None:
        (spec_dir / "design.md").write_text(design_md)
    return db.get_spec(spec.id)


def make_executing_spec(db: Database, **kw) -> Spec:
    return make_spec(db, status=SpecStatus.EXECUTING, **kw)


def make_pending_plan_spec(db: Database, **kw) -> Spec:
    kw.setdefault("plan_yaml", None)
    return make_spec(db, status=SpecStatus.PENDING_PLAN, **kw)


def approve_design(db: Database, spec: Spec, *, complexity: bool = True) -> None:
    """Skip the architect: mark it DONE with the fixture design on disk.

    Writes complexity.json too (the architect always does when its block
    parses): the rotation anchors on it, and without it the anchor drifts —
    see TestRotation in the fault matrix.
    """
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "design.md").write_text(DESIGN_MD)
    if complexity:
        (spec_dir / "complexity.json").write_text(json.dumps(
            {"tier": "medium", "recommended_agent": "implementer",
             "justification": "fixture"}, indent=2) + "\n")
    d._bootstrap_tasks(db, spec)
    arch = db.list_tasks_for_spec_by_role(spec.id, "architect")[0]
    db.update_task_status(arch.id, TaskStatus.DONE)


# ── gate responders ──────────────────────────────────────────────────────────

Decision = tuple[str, str | None]  # ("approved" | "rejected", notes)
Responder = Callable[[ReviewGate], "Decision | None"]


def approve_all(gate: ReviewGate) -> Decision:
    return ("approved", None)


def wait_at(*gate_types: GateType, otherwise: Responder = approve_all) -> Responder:
    """Approve everything except *gate_types*, which stop the drive."""
    def respond(gate: ReviewGate):
        if gate.gate_type in gate_types:
            return None
        return otherwise(gate)
    return respond


def scripted(script: dict[GateType, list[Decision]],
             default: Responder = approve_all) -> Responder:
    """Per-type decision queues; falls back to *default* when a queue empties."""
    queues = {k: list(v) for k, v in script.items()}

    def respond(gate: ReviewGate):
        q = queues.get(gate.gate_type)
        if q:
            return q.pop(0)
        return default(gate)
    return respond


# ── the drive loop ───────────────────────────────────────────────────────────

TERMINAL = (SpecStatus.DONE, SpecStatus.FAILED, SpecStatus.CANCELLED)


@dataclass
class Outcome:
    spec: Spec
    tasks: list[Task]
    gates: list[ReviewGate]
    reason: str          # terminal | waiting | stalled | max_ticks
    ticks: int
    waiting_on: list[ReviewGate] = field(default_factory=list)

    @property
    def status(self) -> SpecStatus:
        return self.spec.status

    def task(self, role: str) -> Task:
        return next(t for t in self.tasks if t.role == role)

    def gates_of(self, gate_type: GateType) -> list[ReviewGate]:
        return [g for g in self.gates if g.gate_type == gate_type]


def _fingerprint(db: Database, spec_id: str, model: FakeModelServer,
                 runner: FakeRunner | None) -> tuple:
    spec = db.get_spec(spec_id)
    tasks = tuple((t.id, t.status, t.retry_count)
                  for t in db.list_tasks_for_spec(spec_id))
    gates = tuple((g.id, g.status) for g in db.list_gates_for_spec(spec_id))
    # A no-verdict disposition that made no model call and no dispatch is
    # still progress (DEV-629): count its events, or a daemon fault that
    # requeues on every tick reads as a stall.
    classified = len(db.list_events_by_kind(
        spec_id=spec_id, kind=EventKind.FAILURE_CLASSIFIED, limit=500))
    return (spec.status, tasks, gates, len(model.calls), classified,
            len(runner.test_calls) if runner else 0,
            len(runner.fetch_calls) if runner else 0)


def drive(db: Database, spec_id: str, model: FakeModelServer,
          responder: Responder = approve_all, *,
          runner: FakeRunner | None = None, max_ticks: int = 60,
          until: Callable[[Spec], bool] | None = None) -> Outcome:
    """Tick the daemon until the spec settles.

    Between ticks every pending gate is put to *responder*; a None decision
    means "a human would look at this" and the drive returns with
    ``reason="waiting"``. Two consecutive ticks that change nothing — no
    status, task, gate, model call or test dispatch — return ``"stalled"``,
    which is how a wedged state machine shows up instead of spinning to the
    budget. An unscripted model call is a test-authoring error and raises.
    """
    reason = "max_ticks"
    ticks = 0
    last = None
    still = 0
    for ticks in range(1, max_ticks + 1):
        spec = db.get_spec(spec_id)
        if spec.status in TERMINAL:
            reason = "terminal"
            ticks -= 1
            break
        if until is not None and until(spec):
            reason = "until"
            ticks -= 1
            break
        pending = db.list_open_gates(spec_id)
        unanswered = []
        for gate in pending:
            decision = responder(gate)
            if decision is None:
                unanswered.append(gate)
                continue
            db.respond_to_gate(gate.id, decision[0], notes=decision[1])
        if unanswered:
            reason = "waiting"
            ticks -= 1
            break
        d.tick(db)
        if model.unscripted:
            raise UnscriptedCall(
                f"daemon called unscripted role(s) {model.unscripted}; "
                f"scripted: {sorted(model.scripts)} defaults: {sorted(model.defaults)}")
        now = _fingerprint(db, spec_id, model, runner)
        still = still + 1 if now == last else 0
        last = now
        if still >= 2:
            reason = "stalled"
            break
    spec = db.get_spec(spec_id)
    if spec.status in TERMINAL and reason not in ("terminal",):
        reason = "terminal"
    return Outcome(spec=spec, tasks=db.list_tasks_for_spec(spec_id),
                   gates=db.list_gates_for_spec(spec_id), reason=reason,
                   ticks=ticks, waiting_on=db.list_open_gates(spec_id))


# ── assertion helpers ────────────────────────────────────────────────────────

def events(db: Database, spec_id: str, kind: EventKind,
           **match: Any) -> list[dict]:
    """Payloads of *kind* events (oldest first) whose fields equal *match*."""
    out = []
    for ev in reversed(db.list_events_by_kind(spec_id=spec_id, kind=kind,
                                              limit=500)):
        payload = json.loads(ev.payload_json or "{}")
        if all(payload.get(k) == v for k, v in match.items()):
            out.append(payload)
    return out


def rejected_gates(db: Database, spec_id: str,
                   gate_type: GateType = GateType.CODE_REVIEW) -> list[ReviewGate]:
    return [g for g in db.list_gates_for_spec(spec_id, gate_type)
            if g.status == GateStatus.REJECTED]


def artifact_paths(db: Database, spec_id: str,
                   kind: ArtifactKind | None = None) -> list[str]:
    return [a.path for a in db.list_artifacts(spec_id, kind=kind)]


def workspace_files(db: Database, spec_id: str) -> dict[str, str]:
    root = db.spec_dir(spec_id)
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and "retry_history" not in p.parts:
            out[str(p.relative_to(root))] = p.read_text(errors="replace")
    return out
