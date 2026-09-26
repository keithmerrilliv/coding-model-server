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
        for n, agent in ((2, "moe_implementer"), (3, "fast_implementer"),
                         (4, "implementer"), (5, "glimmer_implementer")):
            task = _at_retry(db, task, 1)
            rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, agent, feedback=FEEDBACK))
        task = _at_retry(db, task, 1)  # retry 6
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


# ── DEV-676: rotate among the agents that fit; record what actually ran ─────

_WINDOWS = {"implementer": 65536, "deep_implementer": 262144,
            "glimmer_implementer": 65536, "moe_implementer": 118784,
            "fast_implementer": 65536}


def _window(agent):
    return _WINDOWS.get(agent)


class TestRotateAmongFits:
    def test_eligible_agents_keeps_rotation_order_and_drops_small_windows(self):
        assert rp.eligible_agents(70_000, _window) == ["moe_implementer", "deep_implementer"]
        assert rp.eligible_agents(10_000, _window) == rp._IMPLEMENTER_ROTATION

    def test_no_known_window_fits_means_none_not_empty(self):
        assert rp.eligible_agents(300_000, _window) is None
        assert rp.eligible_agents(70_000, lambda a: None) is None

    def test_a_rotation_of_one_repeats_the_only_fit_every_retry(self):
        for retry in (1, 2, 3, 4):
            assert rp._rotation_pick("fast_implementer", retry,
                                     eligible=["deep_implementer"]) == "deep_implementer"

    def test_two_eligible_agents_alternate_in_rotation_order(self):
        # Chain order since DEV-821 puts moe before deep, so the two alternate
        # starting from index 1 of [moe, deep].
        picks = [rp._rotation_pick("fast_implementer", r, eligible=["deep_implementer", "moe_implementer"])
                 for r in (1, 2, 3, 4)]
        assert picks == ["deep_implementer", "moe_implementer", "deep_implementer", "moe_implementer"]

    def test_without_eligibility_the_rotation_is_unchanged(self):
        # DEV-821: retry 1 after `implementer` is Glimmer, not deep.
        assert rp._rotation_pick("implementer", 1) == "glimmer_implementer"
        assert rp._rotation_pick("implementer", 1, eligible=None) == "glimmer_implementer"

    def test_previous_prompt_tokens_reads_the_newest_implementer_generation(self, db, spec_task):
        spec, task = spec_task
        assert rp.previous_prompt_tokens(db, spec.id, "implementer") is None
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "implementer", "prompt_tokens": 40_000})
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "reviewer", "agent": "deep_reviewer", "prompt_tokens": 9_000})
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "deep_implementer", "prompt_tokens": 66_000})
        assert rp.previous_prompt_tokens(db, spec.id, "implementer") == 66_000

    def test_a_reroute_amends_the_record_to_the_agent_that_ran(self, db, spec_task):
        spec, task = spec_task
        task = _at_retry(db, task, 1)
        rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, "fast_implementer"))
        payload = rp.record_reroute(db, spec.id, task, "deep_implementer")
        assert payload["agent"] == "deep_implementer"
        assert payload["assignment"] == "rerouted"
        assert payload["planned_agent"] == "fast_implementer"
        assert payload["retry"] == 1
        assert "does not fit its window" in payload["rationale"]
        latest = rp.previous_plans(db, spec.id, task.id)[0]
        assert latest["agent"] == "deep_implementer"
        from coding_model_autonomous.models import check_event_payload
        assert check_event_payload(EventKind.ATTEMPT_PLANNED, latest) == []

    def test_a_reroute_to_the_planned_agent_records_nothing(self, db, spec_task):
        spec, task = spec_task
        rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, "implementer"))
        assert rp.record_reroute(db, spec.id, task, "implementer") is None
        assert len(rp.previous_plans(db, spec.id, task.id)) == 1

    def test_sole_fit_rationale_names_the_rotation_of_one(self, db, spec_task):
        spec, task = spec_task
        rp.record_attempt_plan(db, spec.id, task, _plan(db, spec, task, "deep_implementer"))
        task = _at_retry(db, task, 1)
        payload = rp.record_attempt_plan(
            db, spec.id, task, _plan(db, spec, task, "deep_implementer", feedback="fix it",
                                     assignment="sole_fit"))
        assert "only agent whose window holds this prompt" in payload["rationale"]
        assert payload["assignment"] == "sole_fit" and payload["planned_agent"] is None
