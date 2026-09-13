"""DEV-631 / DEV-530: every dispatch is planned before it is made.

`AttemptPlan` is what a dispatch pulls on — agent, prompt, feedback,
temperature, environment — recorded as an ATTEMPT_PLANNED event with what
changed since the previous attempt and why. A plan identical to an earlier
attempt's on every lever is a dispatch that has already been tried; the loop
injects the one difference it owns (the next untried agent) or says plainly
that nothing is left. The same record carries DEV-530's difficulty proxy.
"""
import pytest

from coding_model_autonomous import outcome as o
from coding_model_autonomous import retry_policy as rp
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind

STRATEGY = {"repo": "r", "base_ref": "HEAD", "framework": "pytest"}
FEEDBACK = "src/a.py:10:1: error: boom\nsrc/a.py:12:1: error: bang\n"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    return spec, task


def _at_retry(db, task, n):
    for _ in range(n):
        db.increment_task_retry(task.id)
    return db.get_task(task.id)


def _plan(db, spec, task, agent, feedback=None, design="design", **kw):
    return rp.plan_attempt(db, spec.id, task, role="implementer", agent=agent,
                           feedback=feedback, prompt_inputs=(design, ""),
                           strategy=STRATEGY, **kw)


def _verdict(db, spec, task, cls=o.FailureClass.UNAPPLIABLE_EDITS, agent="implementer",
             detail="src/a.py: edit block #1: SEARCH text not found"):
    o._record(db, spec, task, o.Failure(cls, "implementer", "apply", detail,
                                        feedback=FEEDBACK, phase="apply",
                                        extra={"agent": agent}),
              "charge", 0)


class TestLevers:
    def test_the_levers_and_what_changed(self, db, spec_task):
        spec, task = spec_task
        a = _plan(db, spec, task, "implementer")
        b = _plan(db, spec, task, "deep_implementer", feedback="fix it")
        assert set(a.levers()) == {"agent", "prompt_digest", "feedback_digest",
                                   "temperature", "env_digest"}
        assert a.changed_from(rp.asdict(b)) == ["agent", "prompt_digest", "feedback_digest"]
        assert a.changed_from(None) == []
        assert a.feedback_digest == "" and b.feedback_digest != ""

    def test_the_environment_is_a_lever(self, db, spec_task):
        spec, task = spec_task
        a = _plan(db, spec, task, "implementer")
        b = rp.plan_attempt(db, spec.id, task, role="implementer", agent="implementer",
                            feedback=None, prompt_inputs=("design", ""),
                            strategy={**STRATEGY, "base_ref": "v2"})
        assert a.changed_from(rp.asdict(b)) == ["env_digest"]

    def test_the_difficulty_proxy(self, db, spec_task):
        spec, task = spec_task
        _verdict(db, spec, task, agent="fast_implementer")
        task = _at_retry(db, task, 1)
        plan = _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK)
        assert plan.retry == 1
        assert plan.prior_cls == "unappliable_edits"
        assert plan.prior_coarse_key == "unappliable_edits|apply|src/a.py"
        assert plan.prior_agent == "fast_implementer"
        assert plan.prior_outcome == "verdict"
        assert plan.diagnostics == 2 and plan.feedback_chars == len(FEEDBACK)

    def test_the_prior_failure_is_the_one_that_caused_this_attempt(self, db, spec_task):
        """A no-verdict requeue on the CURRENT attempt is newer than the
        verdict that caused it; the proxy wants the cause."""
        spec, task = spec_task
        _verdict(db, spec, task, agent="implementer")           # retry 0 → caused retry 1
        task = _at_retry(db, task, 1)
        o._record(db, spec, task, o.Failure(o.FailureClass.TRANSPORT, "implementer",
                                            "model_call", "ConnectionError"),
                  "requeue", 1)                                  # on retry 1 itself
        plan = _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK)
        assert plan.prior_cls == "unappliable_edits" and plan.prior_agent == "implementer"

    def test_a_first_attempt_has_no_prior(self, db, spec_task):
        spec, task = spec_task
        plan = _plan(db, spec, task, "implementer")
        assert plan.prior_cls is None and plan.diagnostics == 0


class TestRecord:
    def test_the_first_attempt(self, db, spec_task):
        spec, task = spec_task
        payload = rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, "implementer"))
        assert payload["rationale"] == "first attempt"
        assert payload["changed"] == [] and payload["identical_to"] is None
        evs = db.list_events_by_kind(spec_id=spec.id, kind=EventKind.ATTEMPT_PLANNED)
        assert len(evs) == 1 and evs[0].task_id == task.id

    def test_a_retry_says_what_changed_and_after_what(self, db, spec_task):
        spec, task = spec_task
        rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, "implementer"))
        _verdict(db, spec, task, agent="implementer")
        task = _at_retry(db, task, 1)
        payload = rp.record_attempt_plan(
            db, spec.id, task, _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK))
        assert payload["changed"] == ["agent", "prompt_digest", "feedback_digest"]
        assert payload["rationale"].startswith(
            "retry 1 after unappliable_edits (unappliable_edits|apply|src/a.py) by implementer: changed agent")

    def test_a_requeue_on_the_same_attempt_compares_to_the_previous_attempt(self, db, spec_task):
        """Two dispatches of retry 1 (a transport requeue) are the same
        attempt: the second is not 'identical to' the first."""
        spec, task = spec_task
        task = _at_retry(db, task, 1)
        plan = _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK)
        rp.record_attempt_plan(db, spec.id, task, plan)
        payload = rp.record_attempt_plan(db, spec.id, task, plan)
        assert payload["identical_to"] is None


class TestIdenticalDispatch:
    def _history(self, db, spec, task):
        """retry 0 implementer (no feedback); retry 1 deep_implementer + FEEDBACK."""
        rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, "implementer"))
        task = _at_retry(db, task, 1)
        rp.record_attempt_plan(db, spec.id, task,
                               _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK))
        return task

    def test_a_repeat_is_detected_and_the_next_untried_agent_injected(self, db, spec_task):
        spec, task = spec_task
        task = self._history(db, spec, task)
        task = _at_retry(db, task, 4)  # retry 5: the rotation wraps to deep_implementer
        plan = _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK)
        prior = rp.previous_plans(db, spec.id, task.id)
        assert rp.identical_earlier_attempt(plan, prior) == 1
        injected = rp.inject_difference(plan, prior)
        assert injected is not None
        # the next agent along the rotation from deep_implementer whose
        # dispatch (agent + this feedback) nobody has tried yet
        assert injected.agent == "implementer" and injected.assignment == "injected"
        payload = rp.record_attempt_plan(db, spec.id, task, injected, prior)
        assert payload["identical_to"] is None
        assert "injected agent 'implementer'" in payload["rationale"]

    def test_injection_skips_agents_that_already_had_this_dispatch(self, db, spec_task):
        spec, task = spec_task
        task = self._history(db, spec, task)
        for n, agent in ((2, "moe_implementer"), (3, "fast_implementer"), (4, "implementer")):
            task = _at_retry(db, task, 1)
            rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, agent, feedback=FEEDBACK))
        task = _at_retry(db, task, 1)  # retry 5
        plan = _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK)
        prior = rp.previous_plans(db, spec.id, task.id)
        assert rp.inject_difference(plan, prior) is None
        payload = rp.record_attempt_plan(db, spec.id, task, plan, prior)
        assert payload["identical_to"] == 1
        assert "no untried agent is left" in payload["rationale"]

    def test_different_feedback_is_a_different_dispatch(self, db, spec_task):
        spec, task = spec_task
        task = self._history(db, spec, task)
        task = _at_retry(db, task, 4)
        plan = _plan(db, spec, task, "deep_implementer", feedback=FEEDBACK + "and more")
        prior = rp.previous_plans(db, spec.id, task.id)
        assert rp.identical_earlier_attempt(plan, prior) is None
        assert rp.inject_difference(plan, prior) is None


class TestRandomFraction:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.setattr(rp, "ROTATION_RANDOM_FRACTION", 0.0)
        assert all(rp.random_rotation_pick() is None for _ in range(50))

    def test_a_stated_fraction_draws_from_the_rotation(self, monkeypatch):
        monkeypatch.setattr(rp, "ROTATION_RANDOM_FRACTION", 1.0)
        monkeypatch.setattr(rp, "_rng", rp.random.Random(7))
        picks = {rp.random_rotation_pick() for _ in range(200)}
        assert picks == set(rp._IMPLEMENTER_ROTATION)

    def test_half(self, monkeypatch):
        monkeypatch.setattr(rp, "ROTATION_RANDOM_FRACTION", 0.5)
        monkeypatch.setattr(rp, "_rng", rp.random.Random(1))
        n = sum(1 for _ in range(1000) if rp.random_rotation_pick() is not None)
        assert 400 < n < 600
