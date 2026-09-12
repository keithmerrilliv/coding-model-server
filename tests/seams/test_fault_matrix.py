"""One test per fault the live runs have produced (DEV-634).

Each case injects exactly one fault at a seam and asserts what the daemon
does about it. Plain tests pin today's handling — the fixes DEV-538, 581,
620, 622, 624, 637 and 638 shipped. ``xfail(strict=True)`` marks a behaviour
the DEV-628 epic still owes: the test is written against the intended
outcome and its ticket, and it starts passing (and therefore failing the
xfail) the day the fix lands, so the mark has to be lifted with the fix.
"""
from __future__ import annotations

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import (
    EventKind, GateType, SpecStatus, TaskStatus,
)
from coding_model_autonomous import executor

from seam_fakes import (
    CollectionError, Down, Empty, Hang, Inconclusive, MissingChoices,
    PytestPass, Refuse, Reply, Truncated, UnclosedThink, Unreachable,
)
from seam_harness import (
    BAD_EDITS, DAEMON_PATH, DAEMON_STUB, DESIGN_MD, GOOD_EDITS, PLAN_YAML,
    SPEC_MD,
    TEST_FILE, TEST_PATH,
    approve_all, approve_design, architect_reply, design_review_reply, drive, file_blocks,
    events, implementer_edit_reply, implementer_reply,
    make_executing_spec, make_pending_plan_spec, planner_reply, rejected_gates,
    reviewer_reply, scripted, wait_at, workspace_files,
)


def _impl_ready(db, model, runner):
    spec = make_executing_spec(db)
    approve_design(db, spec)
    return spec


def _impl_task(db, spec_id):
    return db.list_tasks_for_spec_by_role(spec_id, "implementer")[0]


# ── the edit ladder ──────────────────────────────────────────────────────────

class TestUnappliableEdits:
    def test_rotates_without_a_human_gate(self, db, model, runner, edit_mode):
        """DEV-581/637: a SEARCH that does not match writes nothing, records
        the complete anchor, and hands the next attempt to the next agent."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer",
                     Reply(implementer_edit_reply(BAD_EDITS, {TEST_PATH: TEST_FILE})),
                     Reply(implementer_edit_reply(GOOD_EDITS, {TEST_PATH: TEST_FILE})))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert out.task("implementer").retry_count == 1
        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "deep_implementer"]
        rej = rejected_gates(db, spec.id)
        assert len(rej) == 1 and "DEV-581" in rej[0].prompt_md
        assert "not found" in rej[0].reviewer_notes.lower() \
            or "no match" in rej[0].reviewer_notes.lower()
        anomaly = events(db, spec.id, EventKind.AGENT_RAN, anomaly="unappliable_edits")
        assert len(anomaly) == 1
        full = anomaly[0]["errors_full"][0]
        assert full["path"] == DAEMON_PATH and full["search"] == BAD_EDITS[DAEMON_PATH][0][0]
        # Nothing was written by attempt 0; attempt 1's apply is what's on disk.
        assert 'role="implementer"' in workspace_files(db, spec.id)[DAEMON_PATH]
        hist = db.spec_dir(spec.id) / "retry_history" / "retry_0"
        assert "apply_errors: 1" in (hist / "implementer_response.md").read_text()
        # The retry prompt carries the diagnostic.
        assert "SEARCH" in model.calls[1].messages[-1]["content"]

    def test_exhaustion_hands_to_synthesis(self, db, model, runner, edit_mode):
        """Six unappliable attempts walk the rotation, then synthesis merges
        them and the result goes to a release gate, never a silent FAIL."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer",
                     Reply(implementer_edit_reply(BAD_EDITS, {TEST_PATH: TEST_FILE})))
        model.script("synthesis", Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        gate = out.waiting_on[0]
        assert gate.gate_type == GateType.RELEASE_APPROVAL
        assert "Synthesized after 5 failed retries" in gate.prompt_md
        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "deep_implementer", "moe_implementer",
            "fast_implementer", "implementer", "deep_implementer"]
        assert [c.model for c in model.calls_for("synthesis")] == ["deep_reviewer"]
        assert out.task("implementer").status == TaskStatus.DONE
        assert len(rejected_gates(db, spec.id)) == 5


class TestRotation:
    def test_anchor_is_stable_with_complexity_json(self, db, model, runner):
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply("not a file block"))
        model.script("synthesis", Reply(implementer_reply()))

        drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "deep_implementer", "moe_implementer",
            "fast_implementer", "implementer", "deep_implementer"]

    def test_anchor_drifts_without_complexity_json(self, db, model, runner):
        """Without complexity.json the pick anchors on task.agent, which the
        previous pick overwrote — so the chain re-bases every retry and
        retries 3 and 4 both land on fast_implementer. Pinned as today's
        behaviour (DEV-640); the rotation's own contract says consecutive
        retries never repeat a model."""
        spec = make_executing_spec(db)
        approve_design(db, spec, complexity=False)
        model.always("implementer", Reply("not a file block"))
        model.script("synthesis", Reply(implementer_reply()))

        drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "deep_implementer", "moe_implementer",
            "fast_implementer", "fast_implementer", "implementer"]


# ── model-server faults ──────────────────────────────────────────────────────

class TestModelServerFaults:
    def test_parse_failure_rotates(self, db, model, runner):
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply("Here is the code:\n```python\nprint(1)\n```"),
                     Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting" and out.task("implementer").retry_count == 1
        rej = rejected_gates(db, spec.id)
        assert rej[0].prompt_md == "## Automated parse-failure retry"
        assert "unparseable" in rej[0].reviewer_notes
        ran = events(db, spec.id, EventKind.AGENT_RAN, role="implementer")
        assert ran[0]["result_kind"] == "ParseError"
        hist = db.spec_dir(spec.id) / "retry_history" / "retry_0"
        assert "- parse_error:" in (hist / "implementer_response.md").read_text()

    def test_413_rotates_without_charging(self, db, model, runner):
        """DEV-624 / DEV-629: a refused request judged nothing. A 413 means
        this agent's window cannot hold the prompt — the next dispatch goes
        to the next agent, and the budget is untouched."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Refuse(413), Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.EXECUTING
        assert out.task("implementer").retry_count == 0
        assert rejected_gates(db, spec.id) == []
        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "deep_implementer"]
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert [(e["cls"], e["outcome"], e["disposition"]) for e in ev] == [
            ("http_refusal", "no_verdict", "rotate")]
        assert ev[0]["status"] == 413 and ev[0]["retry"] == 0

    def test_502_requeues_the_same_agent(self, db, model, runner):
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Refuse(502), Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting" and out.task("implementer").retry_count == 0
        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "implementer"]
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert [(e["cls"], e["disposition"]) for e in ev] == [("http_refusal", "requeue")]

    @pytest.mark.parametrize("fault", [Down(), Hang()])
    def test_transport_failure_parks_without_a_retry(self, db, model, runner, fault):
        spec = _impl_ready(db, model, runner)
        model.script("implementer", fault, Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.EXECUTING
        assert out.task("implementer").retry_count == 0
        assert rejected_gates(db, spec.id) == []
        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "implementer"]

    def test_planner_transport_failure_stays_pending(self, db, model, runner):
        spec = make_pending_plan_spec(db)
        model.script("planner", Down(), Reply(planner_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.PLAN_APPROVAL), runner=runner)

        assert out.status == SpecStatus.PLAN_REVIEW
        ran = events(db, spec.id, EventKind.PLANNER_RAN)
        assert "transient_error" in ran[0] and ran[0]["transient_error"].startswith("ConnectionError")

    def test_empty_architect_output_parks_behind_an_infrastructure_gate(self, db, model, runner):
        """DEV-616/617 shape under DEV-629: empty completions are no verdict.
        The architect is requeued without charge until the cap, then a human
        is asked whether the model is back; approving re-runs it."""
        spec = make_executing_spec(db)
        model.always("architect", Empty())

        out = drive(db, spec.id, model, wait_at(GateType.CLARIFICATION), runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.EXECUTING
        arch = out.task("architect")
        assert arch.retry_count == 0 and arch.status == TaskStatus.BLOCKED_ON_REVIEW
        gate = out.waiting_on[0]
        assert gate.gate_type == GateType.CLARIFICATION and gate.task_id == arch.id
        assert "Infrastructure gate: empty_completion ×6" in gate.prompt_md
        # 6 architect runs × 3 parse attempts each, none charged.
        assert len(model.calls_for("architect")) == 18
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED, role="architect")
        assert [e["disposition"] for e in ev] == ["rotate"] * 5 + ["park"]
        assert all(e["outcome"] == "no_verdict" for e in ev)

        # The model is back: approving the gate re-runs the architect.
        model.scripts.clear(); model.defaults.clear()
        model.script("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))
        out2 = drive(db, spec.id, model, scripted(
            {GateType.CLARIFICATION: [("approved", "server restarted")]},
            default=wait_at(GateType.DESIGN_APPROVAL)), runner=runner)
        assert out2.reason == "waiting"
        assert out2.waiting_on[0].gate_type == GateType.DESIGN_APPROVAL
        assert out2.task("architect").retry_count == 0

    def test_unclosed_think_reads_as_parse_failure(self, db, model, runner):
        spec = make_executing_spec(db)
        model.script("architect", UnclosedThink(), Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))

        out = drive(db, spec.id, model, wait_at(GateType.DESIGN_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        ran = events(db, spec.id, EventKind.AGENT_RAN, role="architect")
        assert [r["result_kind"] for r in ran] == ["ParseError", "ArchitectResult"]
        assert (db.spec_dir(spec.id) / "architect_failed_response_attempt1.txt").is_file()

    def test_truncated_implementer_is_recorded_and_still_gated(self, db, model, runner):
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Truncated(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        trunc = events(db, spec.id, EventKind.OUTPUT_TRUNCATED)
        assert len(trunc) == 1 and trunc[0]["role"] == "implementer"
        assert trunc[0]["agent"] == "implementer" and trunc[0]["max_tokens"] == 16000

    def test_truncated_reviewer_reruns_without_charging_anyone(self, db, model, runner):
        """DEV-629: a length-cut review judged nothing — it used to cost the
        implementer a retry as a soft FAIL after one re-run."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Truncated("<<<REVIEW>>>\n## partial"),
                     Truncated("<<<REVIEW>>>\n## partial again"),
                     Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        assert len(model.calls_for("reviewer")) == 3
        assert out.task("implementer").retry_count == 0
        assert out.task("reviewer").retry_count == 0
        assert rejected_gates(db, spec.id) == []
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED, role="reviewer")
        assert [(e["cls"], e["disposition"]) for e in ev] == [
            ("truncated", "rotate"), ("truncated", "rotate")]

    def test_reviewer_prose_is_a_verdict_rerun_then_soft_fail(self, db, model, runner):
        """Real reviewer output that does not parse: one reviewer re-run on
        the reviewer's own budget, then the implementer pays (unchanged)."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()), Reply(implementer_reply()))
        model.script("reviewer", Reply("Looks fine to me."), Reply("Still prose."),
                     Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        assert out.task("implementer").retry_count == 1
        rej = rejected_gates(db, spec.id)
        assert "could not produce a parseable review" in rej[0].reviewer_notes
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert [(e["role"], e["cls"], e["disposition"]) for e in ev] == [
            ("reviewer", "parse_failure", "charge"),
            ("reviewer", "tests_failed", "charge")]

    def test_design_review_fail_forces_one_revision(self, db, model, runner):
        spec = make_executing_spec(db)
        model.always("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("FAIL", "Missing seam for AC2.")))

        out = drive(db, spec.id, model, wait_at(GateType.DESIGN_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        assert len(model.calls_for("architect")) == 2
        assert len(model.calls_for("design_review")) == 1
        assert "Missing seam for AC2." in model.calls_for("architect")[1].messages[-1]["content"]
        assert out.task("architect").retry_count == 1

    def test_missing_choices_is_no_verdict(self, db, model, runner):
        """DEV-629: a 200 whose body has no choices is a server fault of the
        same class as a 502 — requeued, never terminal."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer", MissingChoices(), Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.EXECUTING
        assert out.task("implementer").retry_count == 0
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert [(e["cls"], e["disposition"]) for e in ev] == [("server_malformed", "requeue")]

    def test_daemon_fault_is_no_verdict_and_caps(self, db, model, runner, monkeypatch):
        """An exception the daemon itself raises inside a runner is not a
        judgement on the code: requeue, then park behind a gate at the cap."""
        import coding_model_server.orchestrator_daemon as d
        spec = _impl_ready(db, model, runner)
        def boom(*a, **k):
            raise KeyError("a daemon bug")
        monkeypatch.setattr(d, "_generate_implementation", boom)

        out = drive(db, spec.id, model, wait_at(GateType.CLARIFICATION), runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.EXECUTING
        assert out.task("implementer").retry_count == 0
        assert "Infrastructure gate: unknown_exception ×6" in out.waiting_on[0].prompt_md
        assert model.calls == []


# ── runner and sandbox faults ────────────────────────────────────────────────

class TestRunnerFaults:
    def test_dead_runner_at_existing_fetch_parks(self, db, model, runner):
        """DEV-620: no model call, no retry burned, resumes when it answers."""
        spec = _impl_ready(db, model, runner)
        runner.fetch_mode = lambda n: "down" if n <= 4 else "ok"
        model.script("implementer", Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert out.task("implementer").retry_count == 0
        parks = events(db, spec.id, EventKind.TEST_RAN, phase="implement_existing_fetch")
        assert len(parks) == 1 and parks[0]["runner_unreachable"] is True
        assert len(model.calls_for("implementer")) == 1

    def test_dead_runner_at_design_fetch_parks_architect(self, db, model, runner):
        spec = make_executing_spec(db)
        runner.fetch_mode = lambda n: "down" if n <= 2 else "ok"
        model.script("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))

        out = drive(db, spec.id, model, wait_at(GateType.DESIGN_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        parks = events(db, spec.id, EventKind.TEST_RAN, phase="design_existing_fetch")
        assert len(parks) == 1
        assert out.task("architect").retry_count == 0

    def test_runner_raising_at_fetch_degrades_soft(self, db, model, runner):
        """A ConnectionError out of the fetch is swallowed: the implementer
        works blind with a warning (the pre-DEV-620 degradation is still the
        behaviour for an exception, as opposed to a problem list)."""
        spec = _impl_ready(db, model, runner)
        runner.fetch_mode = "raise"
        model.script("implementer", Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert events(db, spec.id, EventKind.TEST_RAN, phase="implement_existing_fetch") == []
        prompt = model.calls[0].messages[-1]["content"]
        assert "fixture stand-in for the daemon" not in prompt  # no existing file shown

    def test_unreachable_build_check_requeues_then_escalates(self, db, model, runner):
        """DEV-538/622: three requeues without burning a retry; the fourth
        opens a gate that says the build is unverified."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply()))
        runner.then(Unreachable(), Unreachable(), Unreachable(), Unreachable())

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert out.task("implementer").retry_count == 0
        requeues = events(db, spec.id, EventKind.TEST_RAN,
                          phase="pre_gate_build_check", runner_unreachable=True)
        assert [r["requeue"] for r in requeues] == [1, 2, 3]
        assert len(model.calls_for("implementer")) == 4
        gate = out.waiting_on[0]
        assert "Build check: **could not run" in gate.prompt_md
        assert "mac-runner unreachable" in gate.prompt_md

    def test_unreachable_build_check_recovers(self, db, model, runner):
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply()))
        runner.then(Unreachable(), Unreachable(), PytestPass())

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting" and out.task("implementer").retry_count == 0
        # Each unreachable dispatch leaves two events: the check's own record
        # and the requeue's. The passing one leaves only the check.
        checks = events(db, spec.id, EventKind.TEST_RAN, phase="pre_gate_build_check")
        assert [c.get("runner_unreachable", False) for c in checks] == [
            False, True, False, True, False]
        assert [c["passed"] for c in checks] == [False] * 4 + [True]
        assert (db.spec_dir(spec.id) / "tested_manifest.json").is_file()

    def test_inconclusive_output_is_not_a_pass(self, db, model, runner):
        """Exit 0 with no summary line: the structural guard refuses PASS and
        the gate says so instead of claiming a green build."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        runner.then(Inconclusive())

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        check = events(db, spec.id, EventKind.TEST_RAN, phase="pre_gate_build_check")[0]
        assert check["passed"] is False and check["build_failed"] is False
        assert not (db.spec_dir(spec.id) / "tested_manifest.json").exists()
        assert "passed" not in out.waiting_on[0].prompt_md.split("\n")[3].lower()

    def test_missing_repo_package_is_sandbox_provisioning(self, db, model, runner):
        """DEV-626 shape under DEV-629: the sandbox cannot import the target
        repo's own package. Nothing about the code was judged — requeue, no
        charge, no rotation."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply()))
        runner.then(CollectionError("coding_model_server"), PytestPass())

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert out.task("implementer").retry_count == 0
        assert rejected_gates(db, spec.id) == []
        assert [c.model for c in model.calls_for("implementer")] == [
            "implementer", "implementer"]
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert [(e["cls"], e["disposition"]) for e in ev] == [
            ("sandbox_provisioning", "requeue")]
        assert ev[0]["module"] == "coding_model_server"

    def test_wrong_import_root_is_still_a_build_failure(self, db, model, runner):
        """Run 24's mistake (DEV-644) is the implementer's, not the sandbox's:
        a verdict, charged and rotated."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply()))
        runner.then(CollectionError("src.coding_model_autonomous.executor"), PytestPass())

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert out.task("implementer").retry_count == 1
        rej = rejected_gates(db, spec.id)
        assert "DEV-429" in rej[0].prompt_md and "ModuleNotFoundError" in rej[0].reviewer_notes
        ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert [(e["cls"], e["outcome"], e["disposition"]) for e in ev] == [
            ("build_failure", "verdict", "charge")]
        assert (db.spec_dir(spec.id) / "retry_history" / "retry_0" / "build_failure.txt").is_file()


# ── planner-side and spec-shape faults ───────────────────────────────────────

SWIFT_SPEC = """# Centipede: field forcing

## Summary
Add a forcing strategy.

## test_strategy

```yaml
framework: swift_test
required: true
```
"""

SWIFT_PLAN_NO_REPO = """\
title: Centipede forcing
test_strategy:
  framework: swift_test
  required: true
phases:
  - name: design
    role: architect
  - name: implement
    role: implementer
  - name: test
    role: reviewer
"""


class TestPlannerFaults:
    def test_missing_repo_key_is_bounced_then_accepted(self, db, model, runner):
        """DEV-426: the framework's required keys are checked before review."""
        spec = make_pending_plan_spec(db, spec_md=SWIFT_SPEC)
        fixed = SWIFT_PLAN_NO_REPO.replace("  framework: swift_test",
                                           "  repo: centipede\n  framework: swift_test")
        model.script("planner", Reply(planner_reply(SWIFT_PLAN_NO_REPO)),
                     Reply(planner_reply(fixed)))

        out = drive(db, spec.id, model, wait_at(GateType.PLAN_APPROVAL), runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.PLAN_REVIEW
        clar = out.gates_of(GateType.CLARIFICATION)
        assert len(clar) == 1 and clar[0].prompt_md.startswith("## Plan validation failure")
        assert "`repo` is required" in clar[0].reviewer_notes
        assert "`repo` is required" in model.calls[1].messages[-1]["content"]

    def test_missing_repo_key_fails_after_two_rounds(self, db, model, runner):
        spec = make_pending_plan_spec(db, spec_md=SWIFT_SPEC)
        model.always("planner", Reply(planner_reply(SWIFT_PLAN_NO_REPO)))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.FAILED
        assert len(model.calls_for("planner")) == 3
        assert len(out.gates_of(GateType.CLARIFICATION)) == 2

    def test_planner_parse_failure_rerolls_then_fails(self, db, model, runner):
        spec = make_pending_plan_spec(db)
        model.always("planner", Reply("I think the plan should have three phases."))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.FAILED
        assert len(model.calls_for("planner")) == 2
        ran = events(db, spec.id, EventKind.PLANNER_RAN)
        assert any("error" in r for r in ran)


NO_TABLE_SPEC = SPEC_MD.split("| Path | Action |")[0] + "\n(no change-surface table)\n"
NO_OUTPUTS_PLAN = PLAN_YAML.replace("    outputs:\n", "    outputs_omitted:\n")


class TestSpecShapeFaults:
    def test_zero_candidates_skip_the_fetch_and_disarm_edit_mode(self, db, model, runner, edit_mode):
        """No table rows and no plan outputs: nothing to fetch, so the
        implementer is prompted for whole files and its whole-file rewrite of
        an existing path is written as-is (DEV-492's hazard, still open when
        the spec names nothing)."""
        spec = make_executing_spec(db, spec_md=NO_TABLE_SPEC, plan_yaml=NO_OUTPUTS_PLAN)
        approve_design(db, spec)
        model.script("implementer", Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        fetched = [paths for repo, paths, _ in runner.fetch_calls if DAEMON_PATH in paths]
        assert fetched == []
        prompt = model.calls[0].messages[-1]["content"]
        assert "## File modes" not in prompt
        # The parser strips the trailing newline; the ledger restores it
        # (DEV-641, written by run 24).
        from seam_harness import DAEMON_STUB_IMPLEMENTED
        assert workspace_files(db, spec.id)[DAEMON_PATH] == DAEMON_STUB_IMPLEMENTED

    def test_reviewer_same_path_write_is_renamed(self, db, model, runner):
        """DEV-602 / DEV-642: the reviewer's file at the implementer's path
        lands at a sibling path; the certified artifact and its manifest
        hash are untouched, both suites run, and the gate says so."""
        import hashlib
        import json
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply(
            "PASS", tests={TEST_PATH: TEST_FILE + "\n# reviewer rewrote this\n"})))

        out = drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        files = workspace_files(db, spec.id)
        assert files[TEST_PATH] == TEST_FILE
        renamed = "tests/test_reviewer_existing_fetch_role_log.py"
        assert "reviewer rewrote this" in files[renamed]
        manifest = json.loads(files["tested_manifest.json"])
        assert manifest[TEST_PATH] == hashlib.sha256(files[TEST_PATH].encode()).hexdigest()
        gate = out.waiting_on[0]
        assert "REVIEWER WRITES REDIRECTED" in gate.prompt_md and renamed in gate.prompt_md
        anomaly = events(db, spec.id, EventKind.AGENT_RAN, anomaly="artifact_ledger")
        assert anomaly[0]["renamed"] == [{"path": TEST_PATH, "written_as": renamed,
                                          "prior_role": "implementer"}]
        rows = {a.path: a.role for a in db.list_artifacts(spec.id)}
        assert rows[TEST_PATH] == "implementer" and rows[renamed] == "reviewer"

    def test_reviewer_same_path_write_is_refused_under_refuse_policy(
            self, db, model, runner, monkeypatch):
        from coding_model_autonomous import workspace
        monkeypatch.setattr(workspace, "COLLISION_POLICY", workspace.CollisionPolicy.REFUSE)
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply(
            "PASS", tests={TEST_PATH: TEST_FILE + "\n# reviewer rewrote this\n"})))

        out = drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        files = workspace_files(db, spec.id)
        assert files[TEST_PATH] == TEST_FILE
        assert not any("reviewer rewrote this" in c for c in files.values())
        assert "REFUSED (collision policy: refuse)" in out.waiting_on[0].prompt_md


BIG_DAEMON = "".join(f"def helper_{i}(x):\n    return x + {i}\n\n\n" for i in range(50))
STUB_DAEMON = "def _record_tested_manifest(spec_dir, files, payload):\n    return None\n"


class TestSynthesisLedger:
    def test_synthesis_stub_over_repo_file_is_refused(self, db, model, runner, edit_mode):
        """DEV-636: run 21 v4's 61-line daemon in place of 6,130 lines, after
        five unappliable attempts. The baseline the implementer fetch recorded
        outlives every wiped attempt, so the synthesis write is refused and the
        release gate names it. (A parse-failure exhaustion has no synthesis
        escape hatch today — that inconsistency is DEV-629's.)"""
        runner.repo_files[DAEMON_PATH] = BIG_DAEMON
        spec = _impl_ready(db, model, runner)
        model.always("implementer",
                     Reply(implementer_edit_reply(BAD_EDITS, {TEST_PATH: TEST_FILE})))
        model.script("synthesis", Reply(file_blocks({DAEMON_PATH: STUB_DAEMON,
                                                     TEST_PATH: TEST_FILE})))

        out = drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        files = workspace_files(db, spec.id)
        assert DAEMON_PATH not in files and TEST_PATH in files
        gate = out.waiting_on[0]
        assert "SYNTHESIS WRITES REFUSED" in gate.prompt_md
        assert "refused_shrink" in gate.prompt_md and "against 200 / 50" in gate.prompt_md
        anomaly = events(db, spec.id, EventKind.AGENT_RAN, anomaly="artifact_ledger")
        assert anomaly[-1]["role"] == "synthesizer"
        assert anomaly[-1]["refused"][0]["action"] == "refused_shrink"

    def test_synthesis_corpus_is_only_the_attempts(self, db, model, runner):
        """DEV-639: a sandbox overlay and a pytest cache left in the workspace
        are snapshotted with the attempt but never reach the merge prompt."""
        from seam_fakes import PytestFail, PytestPass
        spec = _impl_ready(db, model, runner)
        spec_dir = db.spec_dir(spec.id)
        overlay_marker = "OVERLAY_MARKER_SHOULD_NOT_BE_IN_PROMPT"

        def plant_then_reply(messages):
            (spec_dir / ".repo_overlay" / "src").mkdir(parents=True, exist_ok=True)
            (spec_dir / ".repo_overlay" / "src" / "x.py").write_text(f"{overlay_marker} = 1\n")
            (spec_dir / ".pytest_cache" / "v").mkdir(parents=True, exist_ok=True)
            (spec_dir / ".pytest_cache" / "v" / "cache").write_text("{}")
            return Reply(implementer_reply())

        model.always("implementer", plant_then_reply)
        model.always("reviewer", Reply(reviewer_reply("FAIL", review="still red")))
        model.script("synthesis", Reply(implementer_reply()))
        runner.default_test = PytestFail()
        # 6 attempts × (build check + reviewer + DEV-563 base run) fail; then
        # synthesis' own run passes.
        runner.tests.extend([PytestFail()] * 18 + [PytestPass()])

        out = drive(db, spec.id, model, approve_all, runner=runner, max_ticks=120)

        synth = model.calls_for("synthesis")
        assert len(synth) == 1, out.reason
        prompt = synth[0].messages[-1]["content"]
        assert overlay_marker not in prompt and ".pytest_cache" not in prompt
        assert "fixture stand-in for the daemon" in prompt  # the attempts' real file
        snaps = sorted((spec_dir / "retry_history").glob("retry_*"))
        assert snaps and (snaps[0] / ".repo_overlay" / "src" / "x.py").is_file()


# ── daemon lifecycle ─────────────────────────────────────────────────────────

class TestCrashRecovery:
    def test_running_task_is_reset_and_capped(self, db, model, runner):
        """DEV-193/558: a task left RUNNING by a crash is reset up to
        MAX_RETRIES times on recovery's own count, then the spec fails."""
        spec = _impl_ready(db, model, runner)
        impl = db.list_tasks_for_spec_by_role(spec.id, "implementer")[0]
        for n in range(1, 7):
            db.update_task_status(impl.id, TaskStatus.RUNNING)
            import coding_model_server.orchestrator_daemon as d
            d.tick(db)
            fresh = db.get_task(impl.id)
            if n <= 5:
                assert fresh.status == TaskStatus.PENDING, n
                assert fresh.retry_count == n
            else:
                assert fresh.status == TaskStatus.FAILED
        assert db.get_spec(spec.id).status == SpecStatus.FAILED
        recov = events(db, spec.id, EventKind.AGENT_RAN, role="crash_recovery")
        assert [r["recovery"] for r in recov] == [1, 2, 3, 4, 5]
        assert model.calls == []


# ── the context stage (DEV-632) ──────────────────────────────────────────────

class TestContextStage:
    def test_one_fetch_serves_the_whole_run(self, db, model, runner):
        """Plan probe, architect, implementer and the post-implement
        normalization all select from ONE runner round-trip (there were at
        least seven per run before the stage), and the run records what the
        fetch held and omitted."""
        spec = make_pending_plan_spec(db)
        model.script("planner", Reply(planner_reply()))
        model.script("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        assert len(runner.fetch_calls) == 1
        _, paths, _ = runner.fetch_calls[0]
        assert DAEMON_PATH in paths and TEST_PATH in paths
        assert "src/coding_model_autonomous/" in paths  # the protected set rides along
        # Both model roles saw the file the plan modifies.
        for role in ("architect", "implementer"):
            prompt = model.calls_for(role)[0].messages[-1]["content"]
            assert "fixture stand-in for the daemon" in prompt
        assert "context.json" in workspace_files(db, spec.id)
        recorded = events(db, spec.id, EventKind.AGENT_RAN, role="context")
        assert len(recorded) == 1
        assert recorded[0]["model_call"] is False
        assert recorded[0]["trigger"] == "plan probe"
        assert recorded[0]["editable"] == [DAEMON_PATH]
        assert any(o.startswith(f"{TEST_PATH} (editable)") for o in recorded[0]["omitted"])

    def test_the_context_survives_the_retry_wipe(self, db, model, runner):
        """A rotated attempt selects from the same fetch: context.json is
        preserved through the retry cleanup like the ledger."""
        from seam_harness import scripted
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()), Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model,
                    scripted({GateType.CODE_REVIEW: [("rejected", "again")]}),
                    runner=runner)

        assert out.status == SpecStatus.DONE
        assert out.task("implementer").retry_count == 1
        assert len(runner.fetch_calls) == 1
        for call in model.calls_for("implementer"):
            assert "fixture stand-in for the daemon" in call.messages[-1]["content"]

    def test_refresh_outage_keeps_the_last_good_fetch(self, db, model, runner, monkeypatch, edit_mode):
        """DEV-544: the runner going away between the architect and the
        implementer no longer strips either section — the implementer works
        from the architect's fetch, is told so, and is not parked."""
        from coding_model_autonomous import context as c
        monkeypatch.setattr(c, "REFRESH_SECONDS", 0)  # re-verify at every role
        spec = make_executing_spec(db)
        runner.fetch_mode = lambda n: "ok" if n == 1 else "down"
        model.script("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))
        model.script("implementer", Reply(implementer_reply()))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        assert len(runner.fetch_calls) >= 2
        assert events(db, spec.id, EventKind.TEST_RAN, phase="implement_existing_fetch") == []
        prompt = model.calls_for("implementer")[0].messages[-1]["content"]
        assert "fixture stand-in for the daemon" in prompt
        assert "## File modes" in prompt  # edit mode stayed armed


# ── the aggregate prompt budget (DEV-633) ────────────────────────────────────

class TestPromptBudget:
    """Run 20 raised AUTONOMOUS_EXISTING_FILES_MAX_CHARS for one big
    modification target; run 21's prompt went past 1 MB into a 413 because
    nothing summed the sections against the window they were headed for."""

    def _oversized_repo(self, runner, chars: int):
        runner.repo_files[DAEMON_PATH] = DAEMON_STUB + "\n# " + "x" * chars
        return runner

    def test_a_prompt_too_big_for_the_pick_escalates_before_it_sheds(
            self, db, model, runner, monkeypatch):
        """300K chars of editable file is ~100K tokens: over implementer's
        64K window, inside deep_implementer's 256K. A bigger window is always
        better than less context, so the dispatch moves and nothing is cut."""
        monkeypatch.setattr(executor, "EXISTING_FILES_MAX_CHARS", 400_000)
        self._oversized_repo(runner, 300_000)
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        call = model.calls_for("implementer")[0]
        assert call.model == "deep_implementer"
        assert "Not shown" not in call.messages[-1]["content"]

    def test_a_prompt_no_window_holds_sheds_and_names_what_it_dropped(
            self, db, model, runner, monkeypatch):
        """900K chars is ~300K tokens — past the largest window even alone.
        The section is droppable, so the prompt is trimmed rather than
        refused, and the implementer is TOLD it did not see the file."""
        monkeypatch.setattr(executor, "EXISTING_FILES_MAX_CHARS", 1_000_000)
        self._oversized_repo(runner, 900_000)
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        prompt = model.calls_for("implementer")[0].messages[-1]["content"]
        assert "Not shown" in prompt and DAEMON_PATH in prompt
        assert "Do not emit them" in prompt
        assert "x" * 900_000 not in prompt

    def test_a_prompt_nothing_can_hold_parks_without_a_model_call(
            self, db, model, runner):
        """The fixed part alone — spec, design, instructions — overflows every
        window, and none of it is droppable. DEV-624 dispatched anyway "for a
        definitive answer"; that is a guaranteed 413 and, under DEV-629, five
        no-verdict round-trips to learn what the sum already knew. Refuse
        before the call: nothing spent, nothing judged, nobody charged."""
        spec = make_executing_spec(db, spec_md=SPEC_MD + "\n\n" + "z" * 3_000_000)
        approve_design(db, spec)
        model.script("implementer", Reply(implementer_reply()))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status is not SpecStatus.FAILED
        assert model.calls_for("implementer") == []
        assert out.task("implementer").retry_count == 0
        classified = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
        assert classified
        assert classified[0]["cls"] == "prompt_too_large"
        assert classified[0]["outcome"] == "no_verdict"

    def test_a_file_bigger_than_its_knob_still_reaches_the_model(
            self, db, model, runner):
        """DEV-648: the knob is a preference, not a ceiling. The one file the
        plan modifies is well past AUTONOMOUS_EXISTING_FILES_MAX_CHARS and
        comfortably inside the window — run 28 showed it to NOBODY, and both
        the architect and the implementer worked blind against the only file
        they were meant to change."""
        self._oversized_repo(runner, 150_000)   # > the 60_000 knob
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        prompt = model.calls_for("implementer")[0].messages[-1]["content"]
        assert "x" * 150_000 in prompt          # the whole file, not a stub
        assert "Not shown" not in prompt

    def test_synthesis_is_not_dispatched_when_it_cannot_emit_its_answer(
            self, db, model, runner):
        """DEV-649: synthesis has no edit mode, so an existing planned output
        costs its full size on the way OUT. Run 28 asked for a 145,825-char
        file inside a 32,000-token budget — every possible answer was a stub,
        and the shrink guard refused two of them 77 minutes later."""
        from seam_fakes import PytestFail
        self._oversized_repo(runner, 150_000)
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply("not a file block"))
        runner.default_test = PytestFail()

        drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL),
              runner=runner, max_ticks=120)

        assert model.calls_for("synthesis") == []    # never dispatched
        assert db.get_spec(spec.id).status is SpecStatus.FAILED
        over = events(db, spec.id, EventKind.AGENT_RAN,
                      anomaly="synthesis_emission_over_budget")
        assert over and over[0]["needed_tokens"] > over[0]["allowed_tokens"]
        assert DAEMON_PATH in over[0]["paths"]

    def test_the_synthesis_corpus_sheds_by_the_sum_not_by_a_413(
            self, db, model, runner, monkeypatch):
        """DEV-572: the merge prompt grows linearly with the attempt count and
        the server refused an oversized body with a 413 — after six failed
        attempts, the worst possible place to lose the run. The budget sheds
        the corpus before the call; the 413 loop stays only as a backstop."""
        from seam_fakes import PytestFail, PytestPass
        # Each attempt carries ~200K chars of file, so six of them are ~1.2M —
        # well past deep_reviewer's 256K-token window.
        fat = {DAEMON_PATH: DAEMON_STUB + "\n# " + "y" * 200_000}
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply(fat)))
        model.always("reviewer", Reply(reviewer_reply("FAIL", review="still red")))
        model.script("synthesis", Reply(implementer_reply()))
        runner.default_test = PytestFail()
        runner.tests.extend([PytestFail()] * 18 + [PytestPass()])

        out = drive(db, spec.id, model, approve_all, runner=runner, max_ticks=120)

        synth = model.calls_for("synthesis")
        assert len(synth) == 1, out.reason      # one call, not a shed loop
        call = synth[0]
        est = len(call.messages[-1]["content"]) // 3
        assert est + call.max_tokens <= d._agent_ctx_limit(call.model)
        # Shed, not emptied: the merge still has something to merge.
        assert "y" * 200_000 in call.messages[-1]["content"]

    def test_one_knob_cannot_raise_another_section(self, db, model, runner, monkeypatch):
        """DEV-627's regression as a whole-run assertion: the operator raises
        the editable knob for a big modification target, and the protected
        section does NOT inherit the room — the window is the ceiling both
        sections share."""
        monkeypatch.setattr(executor, "EXISTING_FILES_MAX_CHARS", 1_000_000)
        monkeypatch.setattr(executor, "PROTECTED_FILES_MAX_CHARS", 1_000_000)
        runner.repo_files["Scaffold/Field.swift"] = "// " + "p" * 400_000
        self._oversized_repo(runner, 400_000)
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        call = model.calls_for("implementer")[0]
        est = len(call.messages[-1]["content"]) // 3
        assert est + call.max_tokens <= d._agent_ctx_limit(call.model)


# ── planned implement outputs (DEV-645) ──────────────────────────────────────

class TestPlannedOutputs:
    """The plan's implement phase declares two outputs. Manifest mode has
    verified its own declared set since DEV-106; single-call mode verified
    nothing, so an attempt that dropped a file was scored downstream as
    whatever the missing file happened to break — behaviour failures on run
    25, and on run 26 nothing at all."""

    def test_a_missing_planned_output_is_charged_not_gated(
            self, db, model, runner):
        """Run 25's shape: only the test file. The attempt is a verdict — the
        budget is charged and the rotation advances — and the human never sees
        a gate for it."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply({TEST_PATH: TEST_FILE})))
        model.script("synthesis", Reply(implementer_reply()))

        drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert _impl_task(db, spec.id).retry_count >= 1
        notes = [g.reviewer_notes or "" for g in rejected_gates(db, spec.id)]
        assert notes and DAEMON_PATH in notes[0]
        assert "missing" in notes[0].lower()
        anomaly = events(db, spec.id, EventKind.AGENT_RAN,
                         anomaly="missing_planned_outputs")
        assert anomaly and anomaly[0]["missing"] == [DAEMON_PATH]

    def test_no_build_check_is_dispatched_for_an_incomplete_attempt(
            self, db, model, runner):
        """The verdict is decided before the build check, so the dispatch is
        not spent on a workspace that is missing a file the plan promised —
        on a Swift spec that is a ~300s Mac round-trip."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(implementer_reply({TEST_PATH: TEST_FILE})))
        model.script("synthesis", Reply(implementer_reply()))

        drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        # Every run_tests call up to synthesis belongs to a complete attempt;
        # the incomplete ones dispatched none.
        assert len(runner.test_calls) < len(model.calls_for("implementer"))

    def test_the_feedback_says_which_form_each_missing_file_needs(
            self, db, model, runner, edit_mode):
        """DEV-638's lesson applied to the feedback: an existing file needs
        edit blocks, a new one needs a whole-file block. Run 21 spent five
        rotations aiming SEARCH/REPLACE at files that did not exist."""
        spec = _impl_ready(db, model, runner)
        model.always("implementer", Reply(file_blocks({TEST_PATH: TEST_FILE})))
        model.script("synthesis", Reply(implementer_reply()))

        drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        notes = rejected_gates(db, spec.id)[0].reviewer_notes or ""
        # The daemon path exists at base_ref (the runner serves it), so it is
        # an edit target, not a whole-file emit.
        assert f"### {DAEMON_PATH}" in notes
        assert "SEARCH/REPLACE" in notes
        assert "no changes needed" in notes

    def test_a_complete_attempt_is_untouched(self, db, model, runner):
        """The other half of the contract: both planned files present means
        the check is silent and the run proceeds exactly as before."""
        spec = _impl_ready(db, model, runner)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        assert events(db, spec.id, EventKind.AGENT_RAN,
                      anomaly="missing_planned_outputs") == []
        assert runner.test_calls  # the build check did run


# ── the design-review revision loop (DEV-647) ────────────────────────────────

class TestDesignRevision:
    """Run 26: the reviewer FAILed the design, the architect produced an
    81-line revision, and the ledger's emptying guard refused it because the
    first design had quoted a `def` signature and the revision had not. One
    WARNING later the daemon said "architect done" and opened the gate over
    the document the reviewer had just failed."""

    # The shape that bit: the first design quotes a signature, the revision is
    # prose. Both are perfectly good designs; only their declaration counts
    # differ, which is an accident of authoring style. (The bare fixture
    # design scores 0, so the guard could never fire on it — the quoted
    # signature below is what makes this the run-26 sequence.)
    WITH_CODE = DESIGN_MD + (
        "\n## API\n\n```python\n"
        "def is_placeholder_path(rel_path: str) -> bool: ...\n```\n")
    PROSE_ONLY = ("# Architecture (revised)\n\n## Overview\n\n"
                  + "The revision the reviewer asked for, in prose.\n" * 30
                  + "\n## File Structure\n\n"
                  + f"- {DAEMON_PATH}\n- {TEST_PATH}\n")

    def test_the_revision_reaches_the_gate(self, db, model, runner):
        spec = make_executing_spec(db)
        model.script("architect",
                     Reply(architect_reply(self.WITH_CODE)),
                     Reply(architect_reply(self.PROSE_ONLY)))
        model.script("design_review", Reply(design_review_reply("FAIL", "redo it")))

        out = drive(db, spec.id, model, wait_at(GateType.DESIGN_APPROVAL), runner=runner)

        assert out.waiting_on[0].gate_type == GateType.DESIGN_APPROVAL
        # The document a human is about to approve is the REVISION, not the
        # one the reviewer failed.
        design = workspace_files(db, spec.id)["design.md"]
        assert "revised" in design
        assert design.startswith("# Architecture (revised)")
        assert not events(db, spec.id, EventKind.AGENT_RAN,
                          anomaly="design_write_refused")

    def test_a_refused_design_write_stops_the_run_instead_of_gating(
            self, db, model, runner, monkeypatch):
        """The content guards no longer reach a DESIGN artifact, so this is
        defence in depth: if some later guard does refuse a design, the run
        must not proceed over the bytes still on disk."""
        from coding_model_autonomous import workspace as ws
        spec = make_executing_spec(db)
        model.always("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))
        monkeypatch.setattr(ws, "is_placeholder_path", lambda p: p == "design.md")

        out = drive(db, spec.id, model, wait_at(GateType.DESIGN_APPROVAL),
                    runner=runner, max_ticks=40)

        assert out.reason != "waiting" or not [
            g for g in out.waiting_on if g.gate_type == GateType.DESIGN_APPROVAL]
        refused = events(db, spec.id, EventKind.AGENT_RAN,
                         anomaly="design_write_refused")
        assert refused and refused[0]["action"] == "refused_placeholder"
        assert db.get_spec(spec.id).status is not SpecStatus.DONE
