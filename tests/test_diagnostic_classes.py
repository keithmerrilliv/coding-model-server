"""DEV-529: build failures carry a closed-set class, the files and the
symbols they name, beside the raw text — and DEV-541's persistence check
matches by class and symbol as well as by exact message (DEV-509 option 3).
"""
import pytest

from coding_model_autonomous import outcome as o
from coding_model_autonomous.db import Database

W = "/tmp/wt-1/Sources/CentipedeCore/World.swift"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


class TestClasses:
    @pytest.mark.parametrize("msg,cls", [
        # the five classes from runs 1–7, in the ticket's words
        ("type 'Mushroom' does not conform to protocol 'Equatable'", "missing_conformance"),
        ("'mutating' is not valid on instance methods in classes", "mutability"),
        ("cannot assign to property: 'type' is a 'let' constant", "mutability"),
        ("cannot find 'World' in scope", "undeclared_type"),
        ("cannot find type 'SeededRNG' in scope", "undeclared_type"),
        ("no such module 'CentipedeCore'", "file_placement"),
        ("No module named 'coding_model_autonomous'", "file_placement"),
        ("value of type 'Segment' has no member 'resolve'", "cross_file_drift"),
        ("incorrect argument label in call (have 'x:', expected 'at:')", "cross_file_drift"),
        # the Python shapes of the same defects
        ("name 'helper' is not defined", "undeclared_type"),
        ("f() got an unexpected keyword argument 'role'", "cross_file_drift"),
        ("'tuple' object does not support item assignment", "mutability"),
        ("unsupported operand type(s) for +: 'int' and 'str'", "missing_conformance"),
        # unrecognised
        ("expected ';' after expression", "other"),
        ("", "other"),
    ])
    def test_each_class(self, msg, cls):
        assert o.classify_diagnostic(msg) == cls

    def test_never_raises(self):
        assert o.classify_diagnostic(None) == "other"  # type: ignore[arg-type]

    def test_symbols_drop_the_noise(self):
        msgs = ["cannot find 'SeededRNG' in scope",
                "cannot convert value of type 'Int' to 'Int64'",
                "value of type 'World.Segment' has no member 'resolve'"]
        assert o.diagnostic_symbols(msgs) == {"SeededRNG", "Segment", "resolve"}

    def test_cited_files_are_repo_relative(self):
        notes = (f"{W}:238:20: error: cannot find 'hitIndex' in scope\n"
                 f"{W}:241:9: error: boom\n"
                 "/abs/elsewhere/Thing.swift:1:1: error: x\n"
                 "src/pkg/a.py:3:1: error: y\n")
        assert o.cited_files(notes) == ["Sources/CentipedeCore/World.swift", "Thing.swift", "src/pkg/a.py"]


class TestOnTheEvent:
    def test_a_verdict_row_carries_class_files_and_symbols(self, db):
        spec = db.create_spec(title="d", source_md_path="spec.md")
        task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="b")
        fb = (f"{W}:1:1: error: type 'Mushroom' does not conform to protocol 'Equatable'\n"
              f"{W}:9:9: error: cannot find 'Segment' in scope\n")
        o._record(db, spec, task, o.Failure(o.FailureClass.BUILD_FAILURE, "implementer",
                                            "build_check", "x", feedback=fb), "charge", 0)
        ev = db.list_events_by_kind(spec_id=spec.id, kind=o.EventKind.FAILURE_CLASSIFIED)[0]
        p = o._payload(ev)
        assert p["diagnostic_classes"] == ["missing_conformance", "undeclared_type"]
        assert p["cited_files"] == ["Sources/CentipedeCore/World.swift"]
        assert p["symbols"] == ["Mushroom", "Segment"]
        assert len(p["diagnostics"]) == 2  # the raw text stays

    def test_no_diagnostics_no_classes(self, db):
        spec = db.create_spec(title="d", source_md_path="spec.md")
        task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="b")
        o._record(db, spec, task, o.Failure(o.FailureClass.TRANSPORT, "implementer",
                                            "model_call", "ConnectionError"), "requeue", 1)
        p = o._payload(db.list_events_by_kind(spec_id=spec.id, kind=o.EventKind.FAILURE_CLASSIFIED)[0])
        assert p["diagnostic_classes"] == [] and p["cited_files"] == [] and p["symbols"] == []


def _verdict(db, spec, notes):
    impls = db.list_tasks_for_spec_by_role(spec.id, "implementer")
    task = impls[0] if impls else db.create_task(spec_id=spec.id, agent="implementer",
                                                 role="implementer", title="b")
    o._record(db, spec, task, o.Failure(o.FailureClass.BUILD_FAILURE, "implementer",
                                        "build_check", notes.splitlines()[0], feedback=notes),
              "charge", 0)


class TestPersistenceByClassAndSymbol:
    def test_run6s_alternating_rng_diagnostics_are_one_defect(self, db):
        """DEV-509: 'the improvised RNG is wrong' one attempt, 'there is no
        RNG at all' the next — exact-message intersection is empty, the
        symbol is the same."""
        spec = db.create_spec(title="r6", source_md_path="spec.md")
        _verdict(db, spec, f"{W}:5:5: error: value of type 'SeededRNG' has no member 'nextInt'")
        current = o.diagnostic_messages(f"{W}:7:7: error: cannot find 'SeededRNG' in scope")
        assert o.persistent_diagnostics(db, spec.id, current, lookback=1) == current

    def test_the_same_class_persists_when_the_text_moves(self, db):
        spec = db.create_spec(title="r7", source_md_path="spec.md")
        _verdict(db, spec, f"{W}:1:1: error: 'mutating' is not valid on instance methods in classes")
        current = o.diagnostic_messages(f"{W}:2:2: error: cannot assign to property: 'x' is a 'let' constant")
        assert o.persistent_diagnostics(db, spec.id, current, lookback=1) == current

    def test_other_never_matches_by_class(self, db):
        spec = db.create_spec(title="x", source_md_path="spec.md")
        _verdict(db, spec, f"{W}:1:1: error: expected ';' after expression")
        current = o.diagnostic_messages(f"{W}:2:2: error: unterminated string literal")
        assert o.persistent_diagnostics(db, spec.id, current, lookback=1) == set()

    def test_a_noise_symbol_does_not_route(self, db):
        spec = db.create_spec(title="x", source_md_path="spec.md")
        _verdict(db, spec, f"{W}:1:1: error: cannot convert value of type 'Int' to 'String'")
        current = o.diagnostic_messages(f"{W}:2:2: error: expected 'Int' somewhere")
        assert o.persistent_diagnostics(db, spec.id, current, lookback=1) == set()

    def test_exact_matches_still_count(self, db):
        spec = db.create_spec(title="x", source_md_path="spec.md")
        _verdict(db, spec, f"{W}:1:1: error: expected ';' after expression")
        current = o.diagnostic_messages(f"{W}:9:9: error: expected ';' after expression")
        assert o.persistent_diagnostics(db, spec.id, current, lookback=1) == current
