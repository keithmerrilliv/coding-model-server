"""DEV-567: CANCELLED wins against concurrent status writers.

An in-flight phase pass used to overwrite an operator's CANCELLED with its own
completion status (run 14: cancelled spec_4f01a833 resurrected to PLAN_REVIEW
and opened an orphan gate). The status write is now a compare-and-swap.
"""

from unittest.mock import MagicMock

from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus

from coding_model_server.orchestrator_daemon import _latest_architect_feedback


def _db(tmp_path):
    return Database(db_path=tmp_path / "t.sqlite",
                    workspace_root=tmp_path / "ws")


class TestCancelledIsTerminal:
    def test_pass_completion_cannot_resurrect_a_cancelled_spec(self, tmp_path):
        db = _db(tmp_path)
        spec = db.create_spec(title="t", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.CANCELLED)
        # The in-flight planner pass completes and writes its result.
        changed = db.update_spec_status(spec.id, SpecStatus.PLAN_REVIEW,
                                        normalized_yaml="title: t")
        assert changed is False
        assert db.get_spec(spec.id).status is SpecStatus.CANCELLED

    def test_force_allows_operator_resurrection(self, tmp_path):
        db = _db(tmp_path)
        spec = db.create_spec(title="t", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.CANCELLED)
        assert db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN,
                                     force=True) is True
        assert db.get_spec(spec.id).status is SpecStatus.PENDING_PLAN

    def test_normal_transitions_still_report_true(self, tmp_path):
        db = _db(tmp_path)
        spec = db.create_spec(title="t", source_md_path="spec.md")
        assert db.update_spec_status(spec.id, SpecStatus.PLAN_REVIEW) is True

    def test_recancelling_a_cancelled_spec_is_fine(self, tmp_path):
        db = _db(tmp_path)
        spec = db.create_spec(title="t", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.CANCELLED)
        assert db.update_spec_status(spec.id, SpecStatus.CANCELLED) is True


class TestHumanFeedbackSurvivesBounces:
    """DEV-569: the human file is read without deletion; transient deduped."""

    def _stub_db(self):
        db = MagicMock()
        db.list_events_by_kind.return_value = []
        return db

    def test_human_notes_survive_two_reads(self, tmp_path):
        spec = MagicMock(id="s1")
        (tmp_path / "human_design_feedback.md").write_text("FIX THE SEAMS")
        first = _latest_architect_feedback(self._stub_db(), spec, tmp_path)
        second = _latest_architect_feedback(self._stub_db(), spec, tmp_path)
        assert "FIX THE SEAMS" in first
        assert "FIX THE SEAMS" in second  # not consumed

    def test_transient_findings_are_consumed_and_combined(self, tmp_path):
        spec = MagicMock(id="s1")
        (tmp_path / "human_design_feedback.md").write_text("HUMAN NOTES")
        (tmp_path / "design_review_feedback.md").write_text("SEAM FINDINGS")
        first = _latest_architect_feedback(self._stub_db(), spec, tmp_path)
        assert "HUMAN NOTES" in first and "SEAM FINDINGS" in first
        second = _latest_architect_feedback(self._stub_db(), spec, tmp_path)
        assert "HUMAN NOTES" in second
        assert "SEAM FINDINGS" not in second  # transient slot consumed once

    def test_duplicate_transient_copy_not_injected_twice(self, tmp_path):
        spec = MagicMock(id="s1")
        (tmp_path / "human_design_feedback.md").write_text("SAME NOTES")
        (tmp_path / "design_review_feedback.md").write_text("SAME NOTES")
        combined = _latest_architect_feedback(self._stub_db(), spec, tmp_path)
        assert combined.count("SAME NOTES") == 1
