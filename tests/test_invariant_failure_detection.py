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
    Failure, FailureClass, Hooks, _record, attempt_agent, coarse_key, dispose,
    invariant_agents, sole_fit_repeats,
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

    # DEV-672 — run 31's first Mac build failure keyed on the runner's
    # absolute worktree path, which carries a per-dispatch hash. Two attempts
    # failing identically in the same file must share one key.
    WT = "/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_c1e1c9ac-{h}/Sources/CentipedeCore/Game.swift:27:14: error: value of type 'Game' has no member 'nextExtraLifeAt'"

    def test_mac_worktree_paths_key_on_the_repo_relative_file(self):
        a = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                    self.WT.format(h="277805d1"), phase="build_check")
        b = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                    self.WT.format(h="7aeae7c4"), phase="build_check")
        assert coarse_key(a) == coarse_key(b)
        assert coarse_key(a).startswith("build_failure|build_check|Sources/CentipedeCore/Game.swift")  # DEV-783: message follows
        assert "worktrees" not in coarse_key(a)

    def test_a_relative_path_is_unchanged_and_an_unknown_layout_keeps_its_basename(self):
        rel = Failure(FailureClass.UNAPPLIABLE_EDITS, "implementer", "apply",
                      "edit block #1: SEARCH text not found in src/a.py", phase="apply")
        assert coarse_key(rel) == "unappliable_edits|apply|src/a.py"
        odd = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                      "/tmp/build-9f/Module/Thing.swift:3:1: error: x", phase="build_check")
        assert coarse_key(odd) == "build_failure|build_check|Thing.swift|x"   # DEV-783: + message


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

    def test_another_roles_event_on_the_task_does_not_win(self, db, spec_task):
        """Run 30: the design review records its AGENT_RAN against the
        architect's task, so the newest event was `reviewer` and the
        architect's own charge was attributed to it. Only the task's own
        role's generations count."""
        spec, _ = spec_task
        arch = db.create_task(spec_id=spec.id, agent="architect",
                              role="architect", title="design")
        arch = db.get_task(arch.id)
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=arch.id,
                        payload={"role": "architect", "agent": "q36_architect"})
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=arch.id,
                        payload={"role": "design_review", "agent": "reviewer"})
        assert attempt_agent(db, spec.id, arch) == "q36_architect"

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


# ── DEV-676: a rotation of one — the second identical failure is invariant ──

class TestSoleFit:
    def _sole_fit_plan(self, db, spec, task, agent="deep_implementer"):
        from coding_model_autonomous import retry_policy as rp
        plan = rp.plan_attempt(db, spec.id, task, role="implementer", agent=agent,
                               feedback="fb", prompt_inputs=("d", ""), strategy={},
                               assignment="sole_fit")
        rp.record_attempt_plan(db, spec.id, task, plan)

    def _fail(self, agent="deep_implementer"):
        return Failure(FailureClass.PARSE_FAILURE, "implementer", "parse",
                       "1 planned implement output(s) not produced: tests/test_x.py",
                       phase="", extra={"agent": agent})

    def test_counts_repeats_only_when_the_latest_plan_is_a_sole_fit(self, db, spec_task):
        spec, task = spec_task
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "deep_implementer"})
        assert sole_fit_repeats(db, spec.id, task, self._fail()) == 0      # no plan
        self._sole_fit_plan(db, spec, task)
        assert sole_fit_repeats(db, spec.id, task, self._fail()) == 1      # first time
        _record(db, spec, task, self._fail(), "charge", 0)
        assert sole_fit_repeats(db, spec.id, task, self._fail()) == 2      # the repeat
        other = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                        "src/x.py:1:1: error: boom", phase="build_check",
                        extra={"agent": "deep_implementer"})
        assert sole_fit_repeats(db, spec.id, task, other) == 1             # a different key

    def test_dispose_hands_a_sole_fit_repeat_to_synthesis(self, db, spec_task):
        spec, task = spec_task
        rev = db.create_task(spec_id=spec.id, agent="reviewer", role="reviewer", title="test")
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "deep_implementer"})
        calls = []
        hooks = Hooks(max_retries=lambda: 5,
                      synthesize=lambda *a: calls.append(a) or None,
                      supervisor=None, reviewer_parse_retries=lambda: 1)
        # attempt 0 fails; attempt 1 is a sole fit on the same agent and fails identically
        d0 = dispose(db, spec, db.get_task(task.id), self._fail(), hooks, reviewer_task=rev)
        assert d0.action == "charge"
        task1 = db.get_task(task.id)
        self._sole_fit_plan(db, spec, task1)
        d1 = dispose(db, spec, task1, self._fail(), hooks, reviewer_task=rev)
        assert d1.action == "synthesize", d1
        assert len(calls) == 1
        assert db.get_task(task.id).retry_count == 1   # not spent to the cap


# ── DEV-783: a build failure's identity is its diagnostic, not only its file ─

def test_two_unrelated_build_failures_in_one_file_are_not_invariant_dev783():
    from coding_model_autonomous.outcome import Failure, FailureClass, coarse_key
    a = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                "/Users/admin/work/ElectricSheep/AudioManager.swift:50:33: error: "
                "type 'AVAudioEngine' has no member 'configurationChangeNotification'")
    b = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                "/Users/admin/work/ElectricSheep/AudioManager.swift:1:1: error: "
                "Expressions are not allowed at the top level")
    assert coarse_key(a) != coarse_key(b)
    assert "audiomanager.swift" in coarse_key(a).lower()


def test_the_same_diagnostic_from_two_worktrees_is_still_invariant_dev783():
    from coding_model_autonomous.outcome import Failure, FailureClass, coarse_key
    a = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                "/w/worktrees/spec_x-aaaa/Sources/Game/Player.swift:2:6: error: "
                "invalid redeclaration of 'Direction'")
    b = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check",
                "/w/worktrees/spec_x-bbbb/Sources/Game/Player.swift:9:6: error:  "
                "Invalid redeclaration of 'Direction'")
    assert coarse_key(a) == coarse_key(b)        # line and case are not identity


def test_non_build_classes_keep_the_class_and_file_key_dev783():
    from coding_model_autonomous.outcome import Failure, FailureClass, coarse_key
    a = Failure(FailureClass.UNAPPLIABLE_EDITS, "implementer", "apply",
                "edit block #1: SEARCH text not found in Sources/A.swift")
    b = Failure(FailureClass.UNAPPLIABLE_EDITS, "implementer", "apply",
                "edit block #5: SEARCH text not found in Sources/A.swift. Closest window: 0.81")
    assert coarse_key(a) == coarse_key(b)        # run 29's shape stays one defect
