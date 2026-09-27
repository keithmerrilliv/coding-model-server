"""DEV-850: the reviewer's device leg.

After a human approves the code at the code-review gate, the reviewer's test
run also runs the Xcode tests on the attached device, with Metal API
validation on — but only when the macOS leg passed and the spec declared
`device_destination`. A leg that did not run leaves the macOS verdict alone
and charges nobody; a leg that ran and failed fails the reviewer's tests.

The LLM call and the runner are mocked, as in test_run_reviewer.py.
"""
import json
from unittest import mock

import pytest
import yaml

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import test_runner as tr
from coding_model_autonomous.executor import ReviewerResult
from coding_model_autonomous.models import (
    ArtifactKind, EventKind, GateType, SpecStatus, TaskStatus)
from coding_model_autonomous.test_strategy import overlay_operator_test_strategy

DEVICE = "platform=visionOS"
MAC_PASS = "Test Suite 'All tests' passed.\n** TEST SUCCEEDED **"


def _plan(device=True):
    ts = {"framework": "xcodebuild_test", "required": True,
          "repo": "electric-sheep", "scheme": "ElectricSheep",
          "destination": "platform=macOS"}
    if device:
        ts["device_destination"] = DEVICE
    return yaml.safe_dump({"test_strategy": ts}, sort_keys=False)


def _setup(db, *, device=True):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec\nbuild a thing")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "Sources").mkdir()
    (spec_dir / "Sources" / "Thing.swift").write_text("struct Thing {}\n")
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.CODE,
                       path="Sources/Thing.swift")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING,
                          normalized_yaml=_plan(device))
    task = db.create_task(spec_id=spec.id, agent="reviewer", role="reviewer",
                          title="review demo")
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


@pytest.fixture
def with_device(db):
    return _setup(db, device=True)


@pytest.fixture
def without_device(db):
    return _setup(db, device=False)


def _run(db, spec, task, spec_dir, results, verdict="PASS"):
    reviewer = ReviewerResult(
        test_files=[("ElectricSheepTests/ThingTests.swift",
                     "import XCTest\nfinal class ThingTests: XCTestCase {}\n")],
        review_md="No issues found.", verdict=verdict, raw="<<<REVIEW>>>")
    with mock.patch.object(d, "call_agent", return_value="raw"), \
            mock.patch.object(d, "parse_reviewer_response", return_value=reviewer), \
            mock.patch.object(d, "build_reviewer_message", return_value=[]), \
            mock.patch.object(d, "_attempt_retry") as retry, \
            mock.patch.object(d, "run_tests", side_effect=results) as rt:
        d._run_reviewer(db, spec, task, spec_dir)
    return rt, retry


def _events(db, spec, kind):
    return [json.loads(e.payload_json or "{}")
            for e in db.list_events_by_kind(spec_id=spec.id, kind=kind, limit=100)]


def _anomalies(db, spec):
    return [p for p in _events(db, spec, EventKind.AGENT_RAN)
            if p.get("anomaly") == "device_leg_unavailable"]


def test_no_device_destination_dispatches_once(db, without_device):
    spec, task, spec_dir = without_device
    rt, retry = _run(db, spec, task, spec_dir, [(True, MAC_PASS)])
    assert rt.call_count == 1
    assert "on_device" not in rt.call_args.kwargs
    ran = _events(db, spec, EventKind.TEST_RAN)
    assert ran == [{"passed": True, "output_chars": len(MAC_PASS)}], (
        "a spec without the key keeps the pre-DEV-850 event, byte for byte")
    assert (spec_dir / "test_output.txt").read_text() == MAC_PASS
    assert not (spec_dir / "device_test_output.txt").exists()
    assert db.list_open_gates(spec.id)[0].gate_type is GateType.RELEASE_APPROVAL


def test_device_leg_runs_after_a_macos_pass(db, with_device):
    spec, task, spec_dir = with_device
    rt, retry = _run(db, spec, task, spec_dir,
                     [(True, MAC_PASS), (True, "** TEST SUCCEEDED ** on device")])
    assert rt.call_count == 2
    first, second = rt.call_args_list
    assert "on_device" not in first.kwargs
    assert first.kwargs["destination"] == "platform=macOS"
    assert second.kwargs["on_device"] is True
    assert second.kwargs["destination"] == DEVICE
    assert second.kwargs["framework"] == "xcodebuild_test"
    assert "device_destination" not in second.kwargs
    retry.assert_not_called()
    gate = db.list_open_gates(spec.id)[0]
    assert gate.gate_type is GateType.RELEASE_APPROVAL
    device_runs = [p for p in _events(db, spec, EventKind.TEST_RAN)
                   if p.get("phase") == "device"]
    assert len(device_runs) == 1
    assert device_runs[0]["passed"] is True
    assert device_runs[0]["destination"] == DEVICE
    assert "PASSED with Metal API validation on" in \
        (spec_dir / "test_output.txt").read_text()


def test_unavailable_device_keeps_the_macos_pass_and_records_an_anomaly(
        db, with_device):
    spec, task, spec_dir = with_device
    unavailable = ("[device-unavailable] device Vision Pro was locked or "
                   "unreachable: The device is locked.\n\nlog...")
    rt, retry = _run(db, spec, task, spec_dir,
                     [(True, MAC_PASS), (False, unavailable)])
    assert rt.call_count == 2
    retry.assert_not_called()
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    gate = db.list_open_gates(spec.id)[0]
    assert gate.gate_type is GateType.RELEASE_APPROVAL
    assert "did NOT run" in gate.prompt_md, (
        "the human approving the release must see the device leg was skipped")
    [anomaly] = _anomalies(db, spec)
    assert anomaly["reason"] == ("device Vision Pro was locked or unreachable: "
                                 "The device is locked.")
    assert anomaly["model_call"] is False
    assert not [p for p in _events(db, spec, EventKind.TEST_RAN)
                if p.get("phase") == "device"], "a leg that did not run is no test run"
    assert not [p for p in _events(db, spec, EventKind.FAILURE_CLASSIFIED)], \
        "nobody is charged for a device that was not there"


def test_device_failure_fails_the_reviewer_tests(db, with_device):
    spec, task, spec_dir = with_device
    # An implementer suite beside the reviewer's arms DEV-563 arbitration.
    (spec_dir / "ElectricSheepTests").mkdir()
    (spec_dir / "ElectricSheepTests" / "ImplTests.swift").write_text(
        "import XCTest\nfinal class ImplTests: XCTestCase {}\n")
    device_out = ("-[MTLDebugRenderCommandEncoder validateCommonDrawErrors:]: "
                  "failed assertion\n** TEST FAILED **")
    rt, retry = _run(db, spec, task, spec_dir,
                     [(True, MAC_PASS), (False, device_out), (True, MAC_PASS)])
    # Two dispatches, not three: arbitration would re-run the macOS base
    # suite, which already passed, and call a device failure reviewer advice.
    assert rt.call_count == 2
    retry.assert_called_once()
    assert db.list_open_gates(spec.id) == []
    written = (spec_dir / "test_output.txt").read_text()
    assert written.startswith(
        f"Device leg ({DEVICE}) failed — Metal API validation on:")
    assert device_out in written
    device_runs = [p for p in _events(db, spec, EventKind.TEST_RAN)
                   if p.get("phase") == "device"]
    assert device_runs == [{"phase": "device", "passed": False,
                            "destination": DEVICE,
                            "output_chars": len(device_out)}]


def test_direct_call_returns_the_header(db, with_device):
    spec, task, spec_dir = with_device
    ts = yaml.safe_load(_plan())["test_strategy"]
    with mock.patch.object(d, "run_tests",
                           side_effect=[(True, MAC_PASS), (False, "boom")]):
        passed, out = d._run_reviewer_tests(db, spec, task, spec_dir,
                                            "xcodebuild_test", ts)
    assert passed is False
    assert out.startswith(f"Device leg ({DEVICE}) failed — Metal API validation on:")


def test_macos_failure_sends_nothing_to_the_device(db, with_device):
    spec, task, spec_dir = with_device
    ts = yaml.safe_load(_plan())["test_strategy"]
    with mock.patch.object(d, "run_tests",
                           side_effect=[(False, "** TEST FAILED **")]) as rt:
        passed, _ = d._run_reviewer_tests(db, spec, task, spec_dir,
                                          "xcodebuild_test", ts)
    assert passed is False
    assert rt.call_count == 1


def test_overlay_restores_a_dropped_device_destination():
    spec_md = ("# Spec\n\n## Test strategy\n\n"
               "- framework: xcodebuild_test\n- repo: electric-sheep\n"
               f"- device_destination: {DEVICE}\n")
    plan = ("title: t\ntest_strategy:\n  framework: xcodebuild_test\n"
            "  repo: electric-sheep\n")
    out = overlay_operator_test_strategy(plan, spec_md, "spec_test")
    assert yaml.safe_load(out)["test_strategy"]["device_destination"] == DEVICE


# ── the dispatch payload (test_runner) ───────────────────────────────────────

def _dispatch(response, **opts):
    """Run one Mac dispatch against a fake runner; return (payload, result)."""
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return response

    def _post(url, json=None, headers=None, timeout=None):
        captured.update(json)
        return _Resp()

    with mock.patch.object(tr, "MAC_RUNNER_API_KEY", "k"), \
            mock.patch.object(tr, "MAC_RUNNER_URL", "http://runner"), \
            mock.patch.object(tr, "_log_runner_version"), \
            mock.patch.object(tr, "_collect_patch_files",
                              return_value=([{"path": "A.swift", "content": "x"}],
                                            None)), \
            mock.patch.object(tr._SESSION, "post", side_effect=_post):
        result = tr.run_tests(mock.MagicMock(name="spec_dir"),
                              framework="xcodebuild_test", repo="es",
                              scheme="ES", **opts)
    return captured, result


def test_payload_has_no_on_device_key_unless_asked():
    for opts in ({}, {"on_device": False}):
        payload, _ = _dispatch({"passed": True, "output": "ok"}, **opts)
        assert "on_device" not in payload


def test_payload_carries_on_device_when_asked():
    payload, (passed, _) = _dispatch(
        {"passed": True, "output": "ok", "on_device": True},
        on_device=True, destination=DEVICE)
    assert payload["on_device"] is True
    assert payload["destination"] == DEVICE
    assert passed is True


def test_a_runner_that_ignored_on_device_is_no_device_verdict():
    """Pydantic drops unknown fields, so an old runner just runs the suite."""
    _, (passed, output) = _dispatch({"passed": True, "output": "ok"},
                                    on_device=True, destination=DEVICE)
    assert passed is False
    assert tr.is_device_unavailable(output)
    assert "predates DEV-850" in output


def test_the_marker_stays_first_whatever_else_the_dispatch_notes():
    _, (passed, output) = _dispatch(
        {"passed": False, "on_device": True,
         "output": "[device-unavailable] device tests are disabled",
         "overwrites": [{"path": "A.swift", "suspected_reconstruction": True,
                         "old_lines": 100, "new_lines": 3}]},
        on_device=True, destination=DEVICE)
    assert passed is False
    assert output.startswith("[device-unavailable] device tests are disabled")


def test_only_xcodebuild_can_run_on_a_device():
    with mock.patch.object(tr, "_run_mac_runner_tests") as mac, \
            mock.patch.object(tr, "_run_local_tests") as local:
        passed, output = tr.run_tests(mock.MagicMock(), framework="swift_test",
                                      repo="c", on_device=True)
    mac.assert_not_called()
    local.assert_not_called()
    assert passed is False
    assert tr.is_device_unavailable(output)
