"""What the daemon does today on the paths that work (DEV-634).

These pin behaviour so a refactor that changes it is caught. Each test
drives the real state machine end to end against scripted fakes and asserts
the observable outcome: statuses, gates, events, artifacts on disk.
"""
from __future__ import annotations

import json

from coding_model_autonomous import (
    ArtifactKind, EventKind, GateStatus, GateType, SpecStatus, TaskStatus,
)

from seam_fakes import Reply
from seam_harness import (
    DAEMON_PATH, TEST_PATH, approve_all, approve_design,
    architect_reply, artifact_paths, design_review_reply, drive, events,
    implementer_reply, make_executing_spec, make_pending_plan_spec,
    planner_reply, reviewer_reply, wait_at, workspace_files,
)


class TestHappyPath:
    def test_full_pipeline_lands_done(self, db, model, runner):
        """plan → design (auto review PASS) → implement → review → DONE."""
        spec = make_pending_plan_spec(db)
        model.script("planner", Reply(planner_reply()))
        model.script("architect", Reply(architect_reply()))
        model.script("design_review", Reply(design_review_reply("PASS")))
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.reason == "terminal" and out.status == SpecStatus.DONE
        assert [t.status for t in out.tasks] == [TaskStatus.DONE] * 3
        assert [g.gate_type for g in out.gates] == [
            GateType.PLAN_APPROVAL, GateType.DESIGN_APPROVAL,
            GateType.CODE_REVIEW, GateType.RELEASE_APPROVAL]
        assert all(g.status == GateStatus.APPROVED for g in out.gates)
        assert [c.role for c in model.calls] == [
            "planner", "architect", "design_review", "implementer", "reviewer"]
        # Two dispatches: the pre-gate build check and the reviewer's suite.
        assert [n for _, _, o in runner.test_calls for n in [o.name]] == [
            "pytest_pass", "pytest_pass"]
        files = workspace_files(db, spec.id)
        assert {"spec.md", "plan.yaml", "design.md", "complexity.json",
                DAEMON_PATH, TEST_PATH, "tests/test_seam.py",
                "implementer_response.md", "tested_manifest.json",
                "test_output.txt", "delivery_report.md"} <= set(files)
        delivery = events(db, spec.id, EventKind.AGENT_RAN, role="delivery")
        assert delivery and delivery[0]["status"] == "skipped"


class TestFromDesign:
    def test_executing_from_approved_design(self, db, model, runner):
        """The implementer/reviewer half on its own, with the DEV-637 retention
        and the DEV-602 manifest visible on disk."""
        spec = make_executing_spec(db)
        approve_design(db, spec)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        assert [c.role for c in model.calls] == ["implementer", "reviewer"]
        assert model.calls[0].model == "implementer"
        assert model.calls[0].max_tokens == 16000
        files = workspace_files(db, spec.id)
        retained = files["implementer_response.md"]
        assert retained.startswith("# Implementer response — attempt 0")
        assert "- agent: implementer" in retained and "- result: ImplementerResult" in retained
        assert "## Raw response" in retained
        manifest = json.loads(files["tested_manifest.json"])
        assert set(manifest) == {DAEMON_PATH, TEST_PATH}
        assert all(len(h) == 64 for h in manifest.values())
        ran = events(db, spec.id, EventKind.AGENT_RAN, role="implementer")
        assert ran[0]["agent"] == "implementer" and ran[0]["result_kind"] == "ImplementerResult"
        assert artifact_paths(db, spec.id, ArtifactKind.CODE) == [DAEMON_PATH, TEST_PATH]

    def test_edit_mode_applies_exact_edits(self, db, model, runner, edit_mode):
        """DEV-581/638: SEARCH/REPLACE against the runner-served base file."""
        from seam_harness import GOOD_EDITS, TEST_FILE, implementer_edit_reply
        spec = make_executing_spec(db)
        approve_design(db, spec)
        model.script("implementer", Reply(implementer_edit_reply(
            GOOD_EDITS, {TEST_PATH: TEST_FILE})))
        model.script("reviewer", Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

        assert out.reason == "waiting"
        gate = out.waiting_on[0]
        assert gate.gate_type == GateType.CODE_REVIEW
        assert "fuzzy" not in gate.prompt_md.lower()
        prompt = model.calls[0].messages[-1]["content"]
        assert "## File modes" in prompt and DAEMON_PATH in prompt
        written = workspace_files(db, spec.id)[DAEMON_PATH]
        assert 'role="implementer"' in written and "to the %s: %s" in written
        ran = events(db, spec.id, EventKind.AGENT_RAN, role="implementer")[0]
        assert ran["edit_tiers"] == {"exact": 2}
        assert ran["edit_applies_nonexact"] == []
        assert runner.fetch_calls[0][0] == "coding-model-server"
        assert DAEMON_PATH in runner.fetch_calls[0][1]


class TestHumanGates:
    def test_code_review_rejection_rotates_with_notes(self, db, model, runner):
        from seam_harness import scripted
        spec = make_executing_spec(db)
        approve_design(db, spec)
        model.script("implementer", Reply(implementer_reply()), Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("PASS")))
        notes = "Rename the keyword to `role` and keep the default."

        out = drive(db, spec.id, model,
                    scripted({GateType.CODE_REVIEW: [("rejected", notes)]}),
                    runner=runner)

        assert out.status == SpecStatus.DONE
        impl = out.task("implementer")
        assert impl.retry_count == 1
        calls = model.calls_for("implementer")
        assert [c.model for c in calls] == ["implementer", "deep_implementer"]
        assert notes in calls[1].messages[-1]["content"]
        assert notes not in calls[0].messages[-1]["content"]
        # Attempt 0 survives the retry cleanup in the history snapshot.
        hist = db.spec_dir(spec.id) / "retry_history" / "retry_0"
        assert (hist / "implementer_response.md").is_file()
        assert (hist / DAEMON_PATH).is_file()

    def test_design_rejection_reruns_architect_with_notes(self, db, model, runner):
        from seam_harness import scripted
        spec = make_executing_spec(db)
        model.script("architect", Reply(architect_reply()), Reply(architect_reply()))
        model.always("design_review", Reply(design_review_reply("PASS")))
        model.script("implementer", Reply(implementer_reply()))
        notes = "Use %-style logging, not f-strings."

        out = drive(db, spec.id, model,
                    scripted({GateType.DESIGN_APPROVAL: [("rejected", notes)]},
                             default=wait_at(GateType.CODE_REVIEW)),
                    runner=runner)

        assert out.reason == "waiting"
        arch = model.calls_for("architect")
        assert len(arch) == 2
        assert notes in arch[1].messages[-1]["content"]
        assert out.task("architect").retry_count == 1
        # Second design review is skipped: retry_count(1) == MAX_REVISIONS.
        assert len(model.calls_for("design_review")) == 1
        # After approval the human's notes are cleared (DEV-569).
        assert not (db.spec_dir(spec.id) / "human_design_feedback.md").exists()

    def test_release_rejection_retries_implementer(self, db, model, runner):
        from seam_harness import scripted
        spec = make_executing_spec(db)
        approve_design(db, spec)
        model.always("implementer", Reply(implementer_reply()))
        model.always("reviewer", Reply(reviewer_reply("PASS")))
        notes = "Not shippable: the test only covers the architect line."

        out = drive(db, spec.id, model,
                    scripted({GateType.RELEASE_APPROVAL: [("rejected", notes)]}),
                    runner=runner)

        assert out.status == SpecStatus.DONE
        assert out.task("implementer").retry_count == 1
        assert [c.role for c in model.calls] == [
            "implementer", "reviewer", "implementer", "reviewer"]
        assert notes in model.calls[2].messages[-1]["content"]


class TestPlanner:
    def test_clarify_round_trip(self, db, model, runner):
        from seam_harness import planner_clarify, scripted
        spec = make_pending_plan_spec(db)
        model.script("planner", Reply(planner_clarify("Which repo?")),
                     Reply(planner_reply()))

        out = drive(db, spec.id, model,
                    scripted({GateType.CLARIFICATION: [("approved", "coding-model-server")]},
                             default=wait_at(GateType.PLAN_APPROVAL)),
                    runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.PLAN_REVIEW
        assert [g.gate_type for g in out.gates] == [
            GateType.CLARIFICATION, GateType.PLAN_APPROVAL]
        assert "coding-model-server" in model.calls[1].messages[-1]["content"]

    def test_plan_rejection_replans_with_notes(self, db, model, runner):
        from seam_harness import scripted
        spec = make_pending_plan_spec(db)
        model.always("planner", Reply(planner_reply()))
        notes = "Split the implement phase in two."

        out = drive(db, spec.id, model,
                    scripted({GateType.PLAN_APPROVAL: [("rejected", notes)]},
                             default=wait_at(GateType.PLAN_APPROVAL)),
                    runner=runner)

        assert out.reason == "waiting" and out.status == SpecStatus.PLAN_REVIEW
        assert len(model.calls_for("planner")) == 2
        assert notes in model.calls[1].messages[-1]["content"]


class TestReviewerVerdicts:
    def test_failing_tests_rotate_implementer(self, db, model, runner):
        from seam_fakes import PytestFail, PytestPass
        spec = make_executing_spec(db)
        approve_design(db, spec)
        model.always("implementer", Reply(implementer_reply()))
        model.always("reviewer", Reply(reviewer_reply("FAIL", review="test_0 fails")))
        # attempt 0: build check pass, reviewer suite fails, DEV-563 base run
        # fails too (so it is a real failure); attempt 1: all green.
        runner.then(PytestPass(), PytestFail(), PytestFail(), PytestPass(), PytestPass())
        model.script("reviewer", Reply(reviewer_reply("FAIL", review="test_0 fails")),
                     Reply(reviewer_reply("PASS")))

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        assert out.task("implementer").retry_count == 1
        rej = [g for g in out.gates_of(GateType.CODE_REVIEW) if g.status == GateStatus.REJECTED]
        assert len(rej) == 1 and rej[0].prompt_md.startswith("## Automated test failure")
        assert "Reviewer verdict: FAIL" in rej[0].reviewer_notes
        assert [o.name for _, _, o in runner.test_calls] == [
            "pytest_pass", "pytest_fail", "pytest_fail", "pytest_pass", "pytest_pass"]
        # The reviewer was reset to PENDING by the disposition, not rescued
        # by crash recovery (which would have charged it a retry).
        assert out.task("reviewer").retry_count == 0
        assert events(db, spec.id, EventKind.AGENT_RAN, role="crash_recovery") == []

    def test_green_suite_with_fail_verdict_goes_to_human(self, db, model, runner):
        """DEV-560: a FAIL verdict over passing tests is adjudicated, not retried."""
        spec = make_executing_spec(db)
        approve_design(db, spec)
        model.script("implementer", Reply(implementer_reply()))
        model.script("reviewer", Reply(reviewer_reply("FAIL", review="style nit")))

        out = drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL), runner=runner)

        assert out.reason == "waiting"
        gate = out.waiting_on[0]
        assert gate.gate_type == GateType.RELEASE_APPROVAL
        assert "DEV-560" in gate.prompt_md
        assert out.task("implementer").retry_count == 0
