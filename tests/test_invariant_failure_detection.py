"""DEV-631: is this the same problem again, and did changing the model help?

The retry loop's one lever is the rotation — a different agent each attempt.
Nothing recorded whether pulling it changed anything, so a spec could spend
its whole budget re-proving the same invariant failure.

Run 29 (spec_f7df9ce0) is why the identity here is NOT `Failure.signature`.
That run produced six verdicts and six DISTINCT signatures while failing the
same way twice, because the signature carries the first line of the detail and
that line carries volatile particulars — which edit block missed, the
similarity score of the closest window. The two failures below are verbatim
from that run.
"""
import pytest

from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind, SpecStatus
from coding_model_autonomous.outcome import (
    Failure, FailureClass, attempt_agent, coarse_key, invariant_agents,
)

# verbatim from run 29, retries 2 and 4 — one defect, two labels
RUN29_BLOCK1 = ("`src/coding_model_autonomous/executor.py`: edit block #1: "
                "SEARCH text not found in the current file. The SEARCH was:")
RUN29_BLOCK5 = ("`src/coding_model_autonomous/executor.py`: edit block #5: "
                "SEARCH text not found in the current file. Closest window: "
                "0.81 similarity at line 2919, below the 0.95 threshold.")


def _edits(detail):
    return Failure(FailureClass.UNAPPLIABLE_EDITS, "implementer", "apply",
                   detail, phase="apply")


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    return db.get_spec(spec.id), db.get_task(task.id)


class TestCoarseKey:
    def test_run_29s_two_anchor_misses_are_one_identity(self):
        """The whole reason the raw signature cannot be the identity."""
        a, b = _edits(RUN29_BLOCK1), _edits(RUN29_BLOCK5)
        assert a.signature != b.signature          # what the ticket proposed
        assert coarse_key(a) == coarse_key(b)      # what actually identifies it
        assert coarse_key(a) == (
            "unappliable_edits|apply|src/coding_model_autonomous/executor.py")

    def test_a_different_file_is_a_different_problem(self):
        assert coarse_key(_edits(RUN29_BLOCK1)) != coarse_key(
            _edits("`tests/test_x.py`: edit block #1: SEARCH text not found"))

    def test_a_different_class_on_the_same_file_is_a_different_problem(self):
        """Run 29's retry 3 — same file, but the model omitted it rather than
        mis-anchoring. Genuinely a different failure; must not collapse."""
        missing = Failure(
            FailureClass.PARSE_FAILURE, "implementer", "parse",
            "1 planned implement output(s) not produced: "
            "src/coding_model_autonomous/executor.py", phase="parse")
        assert coarse_key(missing) != coarse_key(_edits(RUN29_BLOCK1))

    def test_a_detail_with_no_path_still_keys(self):
        f = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                    "E   SyntaxError: '(' was never closed", phase="build_check")
        assert coarse_key(f) == "build_failure|build_check|"


class TestAttemptAgent:
    def test_reads_the_rotations_pick_off_the_generation_event(self, db, spec_task):
        spec, task = spec_task
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "fast_implementer"})
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "deep_implementer"})
        assert attempt_agent(db, spec.id, task) == "deep_implementer"  # newest

    def test_events_without_an_agent_are_skipped_not_fatal(self, db, spec_task):
        spec, task = spec_task
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "implementer"})
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "model_call": False,
                                 "anomaly": "unappliable_edits"})
        assert attempt_agent(db, spec.id, task) == "implementer"

    def test_no_generation_event_is_empty_not_an_error(self, db, spec_task):
        spec, task = spec_task
        assert attempt_agent(db, spec.id, task) == ""


class TestInvariantAgents:
    def _charge(self, db, spec, task, failure, agent):
        from coding_model_autonomous import outcome
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": agent})
        outcome.record_local_charge(db, spec, task, failure)

    def test_the_same_problem_from_two_agents_is_invariant(self, db, spec_task):
        """Run 29's shape: the rotation changed the model and the outcome did
        not move. THAT is the signal, not two identical strings."""
        spec, task = spec_task
        self._charge(db, spec, task, _edits(RUN29_BLOCK1), "implementer")
        self._charge(db, spec, task, _edits(RUN29_BLOCK5), "deep_implementer")

        agents = invariant_agents(db, spec.id, task, _edits(RUN29_BLOCK5))
        assert sorted(agents) == ["deep_implementer", "implementer"]

    def test_an_unrelated_failure_in_between_does_not_break_the_pattern(
            self, db, spec_task):
        """Run 29's retry 3 landed a DIFFERENT failure between the two anchor
        misses, which is exactly what defeats a 'two CONSECUTIVE' rule."""
        spec, task = spec_task
        self._charge(db, spec, task, _edits(RUN29_BLOCK1), "implementer")
        self._charge(db, spec, task, Failure(
            FailureClass.PARSE_FAILURE, "implementer", "parse",
            "1 planned implement output(s) not produced: "
            "src/coding_model_autonomous/executor.py", phase="parse"),
            "moe_implementer")
        self._charge(db, spec, task, _edits(RUN29_BLOCK5), "deep_implementer")

        assert sorted(invariant_agents(db, spec.id, task, _edits(RUN29_BLOCK5))) == [
            "deep_implementer", "implementer"]

    def test_one_agent_twice_is_not_yet_evidence_of_invariance(self, db, spec_task):
        """The same model failing twice says nothing about the next model."""
        spec, task = spec_task
        self._charge(db, spec, task, _edits(RUN29_BLOCK1), "implementer")
        self._charge(db, spec, task, _edits(RUN29_BLOCK5), "implementer")
        assert invariant_agents(db, spec.id, task, _edits(RUN29_BLOCK5)) == [
            "implementer"]

    def test_a_different_problem_starts_its_own_count(self, db, spec_task):
        """A new key inherits nothing — it counts only the agent on the attempt
        that just produced it, which is one, which is below the threshold."""
        spec, task = spec_task
        self._charge(db, spec, task, _edits(RUN29_BLOCK1), "implementer")
        other = _edits("`tests/test_x.py`: edit block #1: SEARCH text not found")
        assert invariant_agents(db, spec.id, task, other) == ["implementer"]

    def test_the_current_attempts_agent_counts(self, db, spec_task):
        """dispose runs BEFORE the failure is recorded, so the agent that just
        produced it is not yet in the stream. Without counting it the check
        needs three agents to notice two — which is what the seam case caught."""
        spec, task = spec_task
        self._charge(db, spec, task, _edits(RUN29_BLOCK1), "implementer")
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "deep_implementer"})
        # deep_implementer's failure has NOT been recorded yet
        assert sorted(invariant_agents(db, spec.id, task, _edits(RUN29_BLOCK5))) == [
            "deep_implementer", "implementer"]


def test_every_classification_records_the_key_and_the_agent(db, spec_task):
    """DEV-631's prerequisite, and DEV-530's: the stream must say which agent
    produced each failure. Before this only the transport and truncation
    classes carried one."""
    from coding_model_autonomous import outcome
    spec, task = spec_task
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer", "agent": "deep_implementer"})
    outcome.record_local_charge(db, spec, task, _edits(RUN29_BLOCK1))

    p = [e.payload for e in db.list_events_by_kind(
             spec_id=spec.id, kind=EventKind.FAILURE_CLASSIFIED, limit=5)][0]
    assert p["agent"] == "deep_implementer"
    assert p["coarse_key"] == (
        "unappliable_edits|apply|src/coding_model_autonomous/executor.py")
    assert p["signature"].startswith("unappliable_edits:")  # kept for diagnostics
