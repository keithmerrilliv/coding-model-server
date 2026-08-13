"""Local static Swift pre-checks — DEV-512.

Two statically-decidable Swift errors were among the pipeline's largest single
error signatures, and each cost a full manifest build plus a ~300s Mac dispatch
to discover:

  * `invalid redeclaration of 'X'` — the same top-level type in two files.
  * `'mutating' is not valid on instance methods in classes`.

These pin the pure detectors (positive AND negative for each) and the
orchestrator wiring: a hit fails the pass locally and routes the diagnostic
back to the implementer through the same `build_reason` channel a real build
failure uses, WITHOUT dispatching to the Mac.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import swift_prechecks as sp
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult
from coding_model_autonomous.models import (
    GateStatus, GateType, SpecStatus, TaskStatus,
)


# ── (a) duplicate top-level declarations ─────────────────────────────────────

def test_direction_redeclaration_across_two_generated_files():
    """The tonight case: a generated Player.swift redeclaring a type an
    existing CentipedeChain.swift already owns."""
    gen = [("Sources/Game/Player.swift",
            "import Foundation\n"
            "enum Direction { case up, down }\n"
            "struct Player { var facing: Direction }\n")]
    ctx = [("Sources/Game/CentipedeChain.swift",
            "public enum Direction { case left, right }\n")]
    result = sp.run_swift_prechecks(gen, ctx)
    assert result.failed()
    assert result.summary() == "invalid redeclaration of 'Direction'"
    report = result.report()
    # Both paths are named — the offender and the previous declaration.
    assert "Sources/Game/Player.swift:2:1: error:" in report
    assert "Sources/Game/CentipedeChain.swift:1:1: note:" in report


def test_same_type_in_two_generated_files_fires_once():
    gen = [("A.swift", "struct Foo {}\n"), ("B.swift", "struct Foo {}\n")]
    vs = sp.duplicate_type_declarations(gen)
    assert len(vs) == 1                       # one diagnostic, not two
    assert vs[0].message == "invalid redeclaration of 'Foo'"
    assert vs[0].path == "A.swift"            # reported on the first site
    assert ("B.swift", 1) in vs[0].notes      # naming the other


def test_same_type_twice_in_one_file():
    gen = [("A.swift", "struct Foo {}\nstruct Foo {}\n")]
    vs = sp.duplicate_type_declarations(gen)
    assert len(vs) == 1
    assert vs[0].line == 1 and ("A.swift", 2) in vs[0].notes


def test_collision_between_two_context_files_is_not_our_problem():
    """A clash purely between pre-existing files is not this pass's doing and
    not its to fix — only a GENERATED offender fires."""
    gen = [("New.swift", "struct Brandnew {}\n")]
    ctx = [("Old1.swift", "struct Shared {}\n"),
           ("Old2.swift", "struct Shared {}\n")]
    assert not sp.run_swift_prechecks(gen, ctx).failed()


def test_nested_same_named_types_in_different_parents_do_not_collide():
    """A nested type is a different type in a different scope — legal Swift."""
    gen = [("A.swift", "struct Outer1 {\n  enum Kind { case a }\n}\n"),
           ("B.swift", "struct Outer2 {\n  enum Kind { case b }\n}\n")]
    assert not sp.run_swift_prechecks(gen).failed()


def test_extension_is_not_a_redeclaration():
    gen = [("A.swift", "struct Foo {}\n"),
           ("B.swift", "extension Foo { func bar() {} }\n")]
    assert not sp.run_swift_prechecks(gen).failed()


def test_regenerating_a_context_file_is_not_a_collision_with_its_old_self():
    """The generated version supersedes the existing one at the same path."""
    gen = [("Sources/Foo.swift", "struct Foo { var x = 1 }\n")]
    ctx = [("Sources/Foo.swift", "struct Foo {}\n")]
    assert not sp.run_swift_prechecks(gen, ctx).failed()


def test_leading_attributes_and_modifiers_before_the_keyword():
    gen = [("A.swift", "@MainActor public final class Widget {}\n"),
           ("B.swift", "final class Widget {}\n")]
    r = sp.run_swift_prechecks(gen)
    assert r.failed() and r.summary() == "invalid redeclaration of 'Widget'"


def test_name_only_in_comment_or_string_does_not_collide():
    gen = [("A.swift", "struct Foo {}\n"),
           ("B.swift", "// struct Foo {}\nlet label = \"struct Foo {}\"\n")]
    assert not sp.run_swift_prechecks(gen).failed()


def test_conditional_compilation_file_is_exempt():
    """A type declared once per `#if os(...)` branch is not a redeclaration;
    without a preprocessor we cannot tell, so the file falls through to swiftc."""
    gen = [("A.swift", "#if os(iOS)\nstruct Foo {}\n#else\nstruct Foo {}\n#endif\n")]
    assert not sp.run_swift_prechecks(gen).failed()


def test_typealias_and_protocol_and_actor_are_all_tracked():
    gen = [("A.swift", "typealias ID = Int\n"), ("B.swift", "typealias ID = String\n"),
           ("C.swift", "protocol Drawable {}\n"), ("D.swift", "protocol Drawable {}\n"),
           ("E.swift", "actor Store {}\n"), ("F.swift", "actor Store {}\n")]
    names = {v.message for v in sp.run_swift_prechecks(gen).violations}
    assert names == {
        "invalid redeclaration of 'ID'",
        "invalid redeclaration of 'Drawable'",
        "invalid redeclaration of 'Store'",
    }


# ── (b) mutating func inside a class ─────────────────────────────────────────

def test_mutating_func_in_class_fires_with_file_and_line():
    src = ("class Counter {\n"
           "    var n = 0\n"
           "    mutating func bump() { n += 1 }\n"
           "}\n")
    vs = sp.mutating_methods_in_classes([("Counter.swift", src)])
    assert len(vs) == 1
    assert vs[0].message == ("'mutating' is not valid on instance methods "
                             "in classes")
    assert vs[0].path == "Counter.swift" and vs[0].line == 3


def test_mutating_func_in_struct_does_not_fire():
    """The required negative case: `mutating` is valid on a value type."""
    src = ("struct Counter {\n"
           "    var n = 0\n"
           "    mutating func bump() { n += 1 }\n"
           "}\n")
    assert sp.mutating_methods_in_classes([("Counter.swift", src)]) == []


def test_mutating_func_in_enum_does_not_fire():
    src = ("enum State {\n    case idle, running\n"
           "    mutating func start() { self = .running }\n}\n")
    assert sp.mutating_methods_in_classes([("State.swift", src)]) == []


def test_mutating_requirement_in_protocol_does_not_fire():
    src = "protocol Resettable {\n    mutating func reset()\n}\n"
    assert sp.mutating_methods_in_classes([("P.swift", src)]) == []


def test_mutating_in_struct_nested_in_class_does_not_fire():
    """The nearest enclosing type is the struct, so it is valid."""
    src = ("class Outer {\n"
           "    struct Inner {\n"
           "        var n = 0\n"
           "        mutating func bump() { n += 1 }\n"
           "    }\n"
           "}\n")
    assert sp.mutating_methods_in_classes([("N.swift", src)]) == []


def test_mutating_in_class_nested_in_struct_fires():
    src = ("struct Outer {\n"
           "    class Inner {\n"
           "        var n = 0\n"
           "        mutating func bump() { n += 1 }\n"
           "    }\n"
           "}\n")
    vs = sp.mutating_methods_in_classes([("N.swift", src)])
    assert len(vs) == 1 and vs[0].line == 4


def test_mutating_inside_a_closure_and_string_do_not_confuse_the_scanner():
    src = ("struct S {\n"
           '    let note = "mutating func x() should be fine in a string"\n'
           "    let f = { (xs: [Int]) in xs.map { $0 } }\n"
           "    mutating func real() {}\n"   # valid: struct
           "}\n")
    assert sp.mutating_methods_in_classes([("S.swift", src)]) == []


def test_word_mutating_that_is_not_a_func_modifier_is_ignored():
    src = "class C {\n    let mutating = 3\n    func f() { _ = mutating }\n}\n"
    assert sp.mutating_methods_in_classes([("C.swift", src)]) == []


# ── negatives that must never fire ───────────────────────────────────────────

def test_clean_swift_passes():
    gen = [("A.swift", "import Foundation\nstruct A { let id: UUID }\n"),
           ("B.swift", "enum B { case x, y }\n")]
    assert not sp.run_swift_prechecks(gen).failed()


def test_non_swift_files_are_ignored():
    gen = [("a.py", "class Foo: pass\nclass Foo: pass\n"),
           ("b.ts", "class Foo {}\n")]
    assert not sp.run_swift_prechecks(gen).failed()


def test_empty_set_is_clean():
    assert not sp.run_swift_prechecks([]).failed()
    assert sp.run_swift_prechecks([]).summary() == ""


# ── the report is shaped like a real swiftc failure (the "same channel") ─────

def test_report_is_recognised_by_the_orchestrators_build_failure_detector():
    """The whole point of the swiftc-shaped report: the existing routing reads
    it exactly as it reads a real build log."""
    report = sp.run_swift_prechecks(
        [("A.swift", "struct Foo {}\n"), ("B.swift", "struct Foo {}\n")]).report()
    # _detect_build_failure treats it as a genuine build failure...
    reason = d._detect_build_failure(report, "swift_test", passed=False)
    assert reason is not None and "invalid redeclaration of 'Foo'" in reason
    # ...and the persistence detector can extract the location-stripped message,
    # so a repeated pre-check failure escalates to the architect just like a
    # repeated compiler diagnostic does.
    assert "invalid redeclaration of 'Foo'" in d._diagnostic_messages(report)


# ── orchestrator wiring: same channel, no Mac dispatch ───────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def impl_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# demo\n")
    (spec_dir / "design.md").write_text("# design\n")
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _run_implementer_with(db, spec, task, spec_dir, files):
    """Drive _run_implementer over *files*; return the _run_tests_with_guard
    mock so callers can assert whether the Mac dispatch happened."""
    result = ImplementerResult(files=files, raw="")
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan",
                           return_value={"test_strategy": {"framework": "swift_test",
                                                           "repo": "centipede"}}), \
         mock.patch.object(d, "_run_tests_with_guard",
                           return_value=(True, "Executed 3 tests")) as run_tests:
        d._run_implementer(db, spec, task, spec_dir)
    return run_tests


def test_duplicate_declaration_fails_locally_before_any_dispatch(db, impl_spec):
    spec, task, spec_dir = impl_spec
    run_tests = _run_implementer_with(db, spec, task, spec_dir, [
        ("Sources/Player.swift", "enum Direction { case up }\n"),
        ("Sources/Chain.swift", "enum Direction { case left }\n"),
    ])

    # The ~300s Mac dispatch never happened.
    run_tests.assert_not_called()

    # Routed back to the implementer on the same channel a build failure uses:
    # one already-answered rejected gate, no human asked.
    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1
    assert gates[0].status is GateStatus.REJECTED
    notes = gates[0].reviewer_notes
    assert "does not compile" in notes
    assert "invalid redeclaration of 'Direction'" in notes
    assert "Sources/Player.swift" in notes and "Sources/Chain.swift" in notes
    assert not [g for g in gates if g.status is GateStatus.PENDING]

    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_task(task.id).retry_count == task.retry_count + 1
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert (spec_dir / "build_failure.txt").exists()


def test_mutating_in_class_fails_locally_before_any_dispatch(db, impl_spec):
    spec, task, spec_dir = impl_spec
    run_tests = _run_implementer_with(db, spec, task, spec_dir, [
        ("Sources/Counter.swift",
         "class Counter {\n    var n = 0\n"
         "    mutating func bump() { n += 1 }\n}\n"),
    ])

    run_tests.assert_not_called()
    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1 and gates[0].status is GateStatus.REJECTED
    assert "not valid on instance methods in classes" in gates[0].reviewer_notes
    assert db.get_task(task.id).status is TaskStatus.PENDING


def test_clean_swift_still_dispatches_to_the_mac(db, impl_spec):
    """The negative wiring case: no violation means the pre-check is invisible
    and the real build check runs as before."""
    spec, task, spec_dir = impl_spec
    run_tests = _run_implementer_with(db, spec, task, spec_dir, [
        ("Sources/A.swift", "struct A { let id: Int }\n"),
        ("Sources/B.swift", "struct B { let id: Int }\n"),
    ])

    run_tests.assert_called_once()
    # A green build opens the normal human review gate.
    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1 and pending[0].gate_type is GateType.CODE_REVIEW
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
