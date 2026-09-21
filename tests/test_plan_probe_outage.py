"""DEV-762: a runner outage during the plan probe parks, it does not terminate.

Run 47's first submission (spec_2a1a0edc) was declared terminal 23 seconds
before the MacBook tunnel came back: the DEV-492 probe caught RunnerOutage
under a bare `except Exception`, reported every declared path as unreadable,
and the DEV-492 guard failed the spec. Unreachable is not unreadable.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.context import RunnerOutage

SPEC_MD = "## Change surface\n\n| path | kind |\n|---|---|\n| `Sources/A.swift` | modify |\n"
PLAN = "test_strategy:\n  repo: demo\n  framework: swift_test\n"


def _spec():
    return SimpleNamespace(id="spec_t", source_md_path="spec.md", normalized_yaml=None)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d, "_declared_file_modifications", lambda md: ["Sources/A.swift"])


def test_probe_recovers_when_the_runner_comes_back_within_the_retries(monkeypatch):
    calls = {"n": 0}

    def flaky(db, spec, spec_md, role, plan):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RunnerOutage("Connection refused")
        return SimpleNamespace(existing=lambda p: "let a = 1")
    monkeypatch.setattr(d, "_spec_context", flaky)
    assert d._unreadable_declared_modifications(_spec(), PLAN, SPEC_MD, db=None) == []
    assert calls["n"] == 3


def test_probe_raises_plan_probe_outage_after_the_bounded_retries(monkeypatch):
    calls = {"n": 0}

    def down(db, spec, spec_md, role, plan):
        calls["n"] += 1
        raise RunnerOutage("Connection reset by peer")
    monkeypatch.setattr(d, "_spec_context", down)
    with pytest.raises(d.PlanProbeOutage):
        d._unreadable_declared_modifications(_spec(), PLAN, SPEC_MD, db=None)
    assert calls["n"] == d.PLAN_PROBE_RETRIES + 1


def test_a_path_absent_at_base_ref_is_still_unreadable(monkeypatch):
    monkeypatch.setattr(d, "_spec_context",
                        lambda *a, **k: SimpleNamespace(existing=lambda p: None))
    assert d._unreadable_declared_modifications(_spec(), PLAN, SPEC_MD, db=None) == ["Sources/A.swift"]


def test_accept_plan_parks_on_probe_outage_instead_of_terminating(monkeypatch, tmp_path):
    (tmp_path / "spec.md").write_text(SPEC_MD)
    monkeypatch.setattr(d, "_overlay_operator_test_strategy", lambda y, md, sid: y)
    monkeypatch.setattr(d, "_validate_test_strategy", lambda y, md: [])
    monkeypatch.setattr(d, "_resolve_plan_phase_paths", lambda db, spec, md, y: (y, []))
    monkeypatch.setattr(d, "ALLOW_UNREAD_FILE_MODIFICATION", False)

    def raise_outage(spec, yaml_text, spec_md, db=None):
        raise d.PlanProbeOutage("127.0.0.1:5050 Connection refused")
    monkeypatch.setattr(d, "_unreadable_declared_modifications", raise_outage)
    block = mock.Mock()
    monkeypatch.setattr(d, "_block_plan_for_unreadable_modification", block)
    db = mock.Mock()
    d._accept_plan(db, _spec(), tmp_path, SimpleNamespace(yaml_text=PLAN))
    block.assert_not_called()                     # no DEV-492 terminal block
    db.update_spec_status.assert_not_called()     # stays PENDING_PLAN
    db.create_gate.assert_not_called()
    payload = db.record_event.call_args.kwargs["payload"]
    assert payload["runner_outage"] and payload["no_verdict"]
    assert payload["phase"] == "plan_probe"
