"""A build-failure repair must be told it is a build failure — DEV-522.

DEV-469 routed build failures into the repair round but left DEV-406's
near-miss wording behind it, so the repair was told its code "already passes
most of its tests" about code that did not compile, under a "## Failing tests"
heading holding compiler diagnostics.

The false flattery is not the damage. "Do not restructure or rewrite passing
behavior" reads as "edit where the error is reported", and a missing
conformance is reported at the USE site and fixed at the DECLARATION. Run 6 of
spec_1ba2db3d (DEV-102) died one word from compiling — `Mushroom` needed
`: Equatable` — because the repair rewrote the test citing the error instead of
the type declaring it. Every source file had compiled.
"""
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus

# The whole error set from spec_1ba2db3d's synthesis, trimmed.
CONFORMANCE_FAILURE = """\
[15/19] Emitting module CentipedeCore
/Users/youruser/.../Tests/CentipedeCoreTests/CentipedeLogicTests.swift:11:32: \
error: operator function '==' requires that 'Mushroom' conform to 'Equatable'
error: fatalError
"""

NEAR_MISS = "Executed 20 tests\n18 passed, 2 failed in 1.2s\n"

FILES = [
    ("Sources/CentipedeCore/Mushroom.swift", "public struct Mushroom {}"),
    ("Tests/CentipedeCoreTests/CentipedeLogicTests.swift", "@Test func a() {}"),
]


def _user_text(messages):
    return next(m["content"] for m in messages if m["role"] == "user")


class TestBuildFailureFraming:
    def test_does_not_claim_the_code_passes_tests(self):
        msg = executor.build_synthesis_repair_message(
            "# spec", "# design", FILES, CONFORMANCE_FAILURE,
            build_diagnostic="error: operator function '==' requires",
        )
        text = _user_text(msg)
        assert "passes most tests" not in text
        assert "already passes most of its tests" not in text

    def test_does_not_ask_the_model_to_protect_passing_behavior(self):
        """The instruction that steered run 6 into the test file."""
        msg = executor.build_synthesis_repair_message(
            "# spec", "# design", FILES, CONFORMANCE_FAILURE,
            build_diagnostic="error: whatever",
        )
        assert "do not restructure or rewrite" not in _user_text(msg).lower()

    def test_names_the_failure_as_a_build_failure(self):
        msg = executor.build_synthesis_repair_message(
            "# spec", "# design", FILES, CONFORMANCE_FAILURE,
            build_diagnostic="error: whatever",
        )
        text = _user_text(msg)
        assert "does NOT compile" in text
        assert "## Compiler diagnostics" in text
        assert "## Failing tests" not in text

    def test_says_the_fix_may_be_in_another_file(self):
        """The conformance case specifically — use site vs declaration site."""
        msg = executor.build_synthesis_repair_message(
            "# spec", "# design", FILES, CONFORMANCE_FAILURE,
            build_diagnostic="error: whatever",
        )
        text = _user_text(msg)
        assert "NOT in the file the diagnostic names" in text
        assert "DECLARES" in text

    def test_still_offers_every_file(self):
        """Not a visibility fix: the repair always saw all of them."""
        msg = executor.build_synthesis_repair_message(
            "# spec", "# design", FILES, CONFORMANCE_FAILURE,
            build_diagnostic="error: whatever",
        )
        text = _user_text(msg)
        for relpath, content in FILES:
            assert relpath in text
            assert content in text


class TestNearMissUnchanged:
    """DEV-406's wording is correct where it is true and must not weaken."""

    def test_near_miss_prompt_is_byte_identical_to_the_original(self):
        msg = executor.build_synthesis_repair_message(
            "# spec", "# design", FILES, NEAR_MISS)
        text = _user_text(msg)
        assert "## Current implementation (passes most tests)" in text
        assert "## Failing tests" in text
        assert (
            "This implementation already passes most of its tests. Fix ONLY "
            "what the failures above require — do not restructure or rewrite "
            "passing behavior. Output <<<FILE: path>>>…<<<END_FILE>>> blocks "
            "for JUST the files you change, each with its complete content."
        ) in text

    def test_omitting_the_diagnostic_keeps_the_near_miss_prompt(self):
        """Default argument — existing callers are unaffected."""
        assert "does NOT compile" not in _user_text(
            executor.build_synthesis_repair_message(
                "# spec", "# design", FILES, NEAR_MISS))


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def synth_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "impl.swift").write_text("struct A {}")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    return db.get_spec(spec.id), impl, spec_dir


def _drive(db, spec, impl, spec_dir, first_output, repair_files):
    """Run _run_synthesis to its repair round, capturing the prompt kwargs."""
    synth = SimpleNamespace(files=[("a.swift", "A"), ("b.swift", "B"),
                                   ("c.swift", "C")])
    repair = SimpleNamespace(files=repair_files)
    seen = {}

    def _build(*args, **kwargs):
        seen.update(kwargs)
        return []

    parsed = iter([synth, repair])
    with mock.patch.object(d, "call_agent", return_value="raw"), \
         mock.patch.object(d, "build_synthesis_message", return_value=[]), \
         mock.patch.object(d, "parse_implementer_response",
                           side_effect=lambda _raw: next(parsed)), \
         mock.patch.object(d, "run_tests",
                           side_effect=[(False, first_output),
                                        (False, first_output)]), \
         mock.patch.object(d.executor, "build_synthesis_repair_message",
                           side_effect=_build):
        d._run_synthesis(db, spec, impl, spec_dir, "swift_test", {})
    return seen


class TestCallSite:
    def test_build_failure_passes_the_diagnostic_through(self, db, synth_spec):
        spec, impl, spec_dir = synth_spec
        seen = _drive(db, spec, impl, spec_dir, CONFORMANCE_FAILURE,
                      [("Tests/CentipedeCoreTests/CentipedeLogicTests.swift", "x")])
        assert seen.get("build_diagnostic")
        assert "Equatable" in seen["build_diagnostic"]

    def test_near_miss_passes_no_diagnostic(self, db, synth_spec):
        spec, impl, spec_dir = synth_spec
        seen = _drive(db, spec, impl, spec_dir, NEAR_MISS, [("a.swift", "x")])
        assert seen.get("build_diagnostic") is None

    def test_event_distinguishes_files_offered_from_files_changed(
            self, db, synth_spec):
        """The ambiguity that made this defect look like a visibility bug."""
        spec, impl, spec_dir = synth_spec
        _drive(db, spec, impl, spec_dir, CONFORMANCE_FAILURE,
               [("Tests/CentipedeCoreTests/CentipedeLogicTests.swift", "x")])
        payloads = (json.loads(e.payload_json)
                    for e in db.list_recent_events(spec_id=spec.id)
                    if e.payload_json)
        payload = next(p for p in payloads
                       if p.get("role") == "synthesis_repair")
        assert payload["files_offered"] == 3
        assert payload["files_changed"] == 1
        assert payload["changed_paths"] == [
            "Tests/CentipedeCoreTests/CentipedeLogicTests.swift"]
        assert "files" not in payload
