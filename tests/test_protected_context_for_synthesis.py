"""Synthesis and repair must see the protected files — DEV-552.

DEV-492 gave the architect and the implementer the protected scaffold as
read-only context so they would stop redeclaring types that already exist. The
two generations reached only after every retry is spent — synthesis and its
repair round — never got it.

Run 10 of DEV-102 died on that. The repair invented
`Sources/CentipedeCore/Field.swift` declaring `public struct Field` while the
protected `GameState.swift` already declared `public enum Field`; every
diagnostic in the final build came from that one file, and no later attempt
could have fixed it, because the file it collides with is one the pipeline is
forbidden to edit.

Two defences here: the prompts now carry the scaffold, and a mechanical check
drops a generated file that is a pure redeclaration regardless of what any
prompt said.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus

# Verbatim from the protected scaffold this spec cannot edit.
GAMESTATE = """\
import Foundation

public enum Field {
    public static let columns = 30
    public static let rows = 30
}

public struct GameState: Sendable {
    public var score: Int = 0
}
"""

# Verbatim shape of what run 10's repair invented.
INVENTED_FIELD = """\
public struct Field {
    public static let columns = 30
    public static let rows = 30
}
"""

PROTECTED = [("Sources/CentipedeCore/GameState.swift", GAMESTATE)]


# ── the detector ─────────────────────────────────────────────────────────────

def test_run10_collision_is_detected_and_marked_total():
    """The exact file that killed run 10."""
    hits = executor.protected_type_collisions(
        [("Sources/CentipedeCore/Field.swift", INVENTED_FIELD)], PROTECTED)
    assert len(hits) == 1
    path, names, total = hits[0]
    assert path == "Sources/CentipedeCore/Field.swift"
    assert names == ["Field"]
    assert total is True, "the file declares nothing else, so it is a pure duplicate"


def test_a_file_declaring_its_own_types_is_untouched():
    ok = [("Sources/CentipedeCore/World.swift", "public struct World {}\n")]
    assert executor.protected_type_collisions(ok, PROTECTED) == []


def test_extending_a_protected_type_is_not_a_redeclaration():
    """`extension Field { … }` is the CORRECT way to add to a protected type."""
    ext = [("Sources/CentipedeCore/FieldExt.swift",
            "extension Field { static let playerZone = 25 }\n")]
    assert executor.protected_type_collisions(ext, PROTECTED) == []


def test_a_nested_type_of_the_same_name_is_not_a_collision():
    """A nested `Field` is a different type in a different scope, and the
    compiler is perfectly happy with it. Flagging it would delete real work."""
    nested = [("Sources/CentipedeCore/World.swift",
               "public struct World {\n    enum Field { case a }\n}\n")]
    assert executor.protected_type_collisions(nested, PROTECTED) == []


def test_partial_collision_is_reported_but_not_total():
    """A file that also declares wanted types must not be silently deleted."""
    mixed = [("Sources/CentipedeCore/World.swift",
              "public struct World {}\npublic struct Field {}\n")]
    hits = executor.protected_type_collisions(mixed, PROTECTED)
    assert len(hits) == 1
    _, names, total = hits[0]
    assert names == ["Field"]
    assert total is False


def test_the_protected_file_itself_is_never_an_offender():
    assert executor.protected_type_collisions(PROTECTED, PROTECTED) == []


def test_non_swift_and_empty_inputs_are_safe():
    assert executor.protected_type_collisions([("a.py", "class Field: pass")],
                                              PROTECTED) == []
    assert executor.protected_type_collisions([], PROTECTED) == []
    assert executor.protected_type_collisions(
        [("A.swift", "struct Field {}")], []) == []


# ── the prompts ──────────────────────────────────────────────────────────────

def test_synthesis_prompt_carries_the_protected_files():
    text = executor.build_synthesis_message(
        "# spec", "# design", [], reference_files=PROTECTED)[-1]["content"]
    assert "GameState.swift" in text
    assert "public enum Field" in text


def test_repair_prompt_carries_the_protected_files():
    """The repair is the last generation of the run — run 10 died here."""
    text = executor.build_synthesis_repair_message(
        "# spec", "# design", [("A.swift", "struct A {}")], "output",
        reference_files=PROTECTED)[-1]["content"]
    assert "GameState.swift" in text
    assert "public enum Field" in text


@pytest.mark.parametrize("empty", [None, []])
def test_prompts_are_byte_identical_without_protected_files(empty):
    """Specs with no protected_paths must see no change at all."""
    assert executor.build_synthesis_message(
        "# spec", "# design", [], reference_files=empty
    ) == executor.build_synthesis_message("# spec", "# design", [])
    assert executor.build_synthesis_repair_message(
        "# spec", "# design", [("A.swift", "x")], "out", reference_files=empty
    ) == executor.build_synthesis_repair_message(
        "# spec", "# design", [("A.swift", "x")], "out")


# ── the drop, through the real write path ────────────────────────────────────

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
                          role="implementer", title="build")
    return db.get_spec(spec.id), db.get_task(task.id)


def test_write_path_drops_the_pure_duplicate(db, spec_task):
    """Replay of run 10: the invented Field.swift never reaches the compiler."""
    spec, task = spec_task
    files = [
        ("Sources/CentipedeCore/World.swift", "public struct World {}\n"),
        ("Sources/CentipedeCore/Field.swift", INVENTED_FIELD),
    ]
    kept = d._normalize_generated_files(db, spec, task, files, "synthesis_repair",
                                        protected_files=PROTECTED)
    paths = [p for p, _ in kept]
    assert "Sources/CentipedeCore/Field.swift" not in paths
    assert "Sources/CentipedeCore/World.swift" in paths


def test_write_path_keeps_a_file_that_also_declares_wanted_types(db, spec_task):
    """Deleting real work to dodge a diagnostic is the worse trade — the build
    fails either way, so keep the file and say so loudly."""
    spec, task = spec_task
    files = [("Sources/CentipedeCore/World.swift",
              "public struct World {}\npublic struct Field {}\n")]
    kept = d._normalize_generated_files(db, spec, task, files, "synthesizer",
                                        protected_files=PROTECTED)
    assert [p for p, _ in kept] == ["Sources/CentipedeCore/World.swift"]


def test_no_protected_files_means_no_behaviour_change(db, spec_task):
    spec, task = spec_task
    files = [("Sources/CentipedeCore/Field.swift", INVENTED_FIELD)]
    assert d._normalize_generated_files(
        db, spec, task, files, "implementer", protected_files=None) == files


def test_a_broken_detector_never_breaks_a_generation(db, spec_task):
    """Fail open: a lint step must not be able to lose a whole generation."""
    spec, task = spec_task
    files = [("Sources/CentipedeCore/Field.swift", INVENTED_FIELD)]
    with mock.patch.object(executor, "protected_type_collisions",
                           side_effect=RuntimeError("boom")):
        assert d._normalize_generated_files(
            db, spec, task, files, "synthesizer",
            protected_files=PROTECTED) == files
