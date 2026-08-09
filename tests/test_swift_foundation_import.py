"""Tests for the deterministic Swift `import Foundation` fix (DEV-540).

The defect: a generated Swift file names `UUID` and omits `import Foundation`,
so nothing compiles. It was the top terminal failure of Centipede runs 7 and 8.
"""
from coding_model_autonomous import executor


def _normalize_one(path: str, content: str):
    """Run one file through normalize_boilerplate; return (content, notes)."""
    out, notes = executor.normalize_boilerplate([(path, content)])
    return dict(out)[path], notes


def test_adds_import_when_uuid_used_without_it():
    src = (
        "public struct CentipedeChain {\n"
        "    public let id: UUID\n"
        "}\n"
    )
    new, notes = _normalize_one("Sources/A/CentipedeChain.swift", src)
    assert new.startswith("import Foundation\n\n")
    assert "public let id: UUID" in new
    assert len(notes) == 1 and "UUID" in notes[0]


def test_noop_when_foundation_already_imported():
    src = "import Foundation\n\nlet id = UUID()\n"
    new, notes = _normalize_one("A.swift", src)
    assert new == src  # byte-for-byte
    assert notes == []


def test_noop_when_no_foundation_symbol_used():
    src = "public enum ChainDirection {\n    case left, right\n}\n"
    new, notes = _normalize_one("A.swift", src)
    assert new == src
    assert notes == []


def test_inserted_before_existing_imports_not_after_them():
    src = "import Testing\n@testable import CentipedeCore\n\nlet d = Date()\n"
    new, _ = _normalize_one("Tests/T.swift", src)
    assert new.splitlines()[0] == "import Foundation"
    # The original imports survive, in order.
    assert new.splitlines()[1:3] == ["import Testing",
                                     "@testable import CentipedeCore"]


def test_inserted_after_header_comment_not_inside_it():
    src = (
        "/// A centipede chain.\n"
        "/// Index 0 is always the head.\n"
        "public struct CentipedeChain {\n"
        "    public let id: UUID\n"
        "}\n"
    )
    new, _ = _normalize_one("A.swift", src)
    lines = new.splitlines()
    assert lines[0] == "/// A centipede chain."
    assert lines[1] == "/// Index 0 is always the head."
    assert lines[2] == "import Foundation"
    assert lines[4] == "public struct CentipedeChain {"


def test_block_comment_header_is_not_split():
    src = (
        "/*\n"
        " * Seed module.\n"
        " */\n"
        "let stamp = Date()\n"
    )
    new, _ = _normalize_one("A.swift", src)
    lines = new.splitlines()
    assert lines[:3] == ["/*", " * Seed module.", " */"]
    assert lines[3] == "import Foundation"


def test_symbol_named_only_in_a_comment_does_not_trigger():
    src = "// Returns a UUID for the caller.\npublic func f() -> Int { 0 }\n"
    new, notes = _normalize_one("A.swift", src)
    assert new == src
    assert notes == []


def test_symbol_named_only_in_a_string_does_not_trigger():
    src = 'public let label = "UUID"\n'
    new, notes = _normalize_one("A.swift", src)
    assert new == src
    assert notes == []


def test_url_literal_with_double_slash_does_not_hide_real_usage():
    # Stripping comments before strings would eat `let id = UUID()` here,
    # because everything after `//` inside the URL would read as a comment.
    src = 'let host = "http://example.com"\nlet id = UUID()\n'
    new, notes = _normalize_one("A.swift", src)
    assert new.startswith("import Foundation\n")
    assert notes and "UUID" in notes[0]


def test_locally_declared_type_is_not_a_foundation_reference():
    src = "public struct Timer {\n    public var ticks: Int\n}\n"
    new, notes = _normalize_one("A.swift", src)
    assert new == src
    assert notes == []


def test_qualified_and_prefixed_names_do_not_trigger():
    src = "let a = MyUUID()\nlet b = registry.Date\n"
    new, notes = _normalize_one("A.swift", src)
    assert new == src
    assert notes == []


def test_reexporting_module_import_suppresses_the_addition():
    # SwiftUI re-exports Foundation, so adding it would be noise.
    src = "import SwiftUI\n\nlet now = Date()\n"
    new, notes = _normalize_one("A.swift", src)
    assert new == src
    assert notes == []


def test_non_swift_files_are_untouched():
    src = "const id = 'UUID';\n"
    new, notes = _normalize_one("src/app.ts", src)
    assert new == src
    assert notes == []


def test_note_lists_every_symbol_that_required_the_import():
    src = "let a: UUID\nlet b: Date\nlet c: URL\n"
    _, notes = _normalize_one("A.swift", src)
    assert len(notes) == 1
    for sym in ("UUID", "Date", "URL"):
        assert sym in notes[0]


def test_run_8_synthesis_repair_output_is_repaired():
    """Regression: the exact shape run 8 died on (spec_63a31526, 2026-08-09).

    The repair round emitted these files with no `import Foundation`, producing
    ten `cannot find type 'UUID' in scope` errors plus two cascading Equatable
    conformance failures.
    """
    files = [
        ("Sources/CentipedeCore/CentipedeChain.swift",
         "public struct CentipedeChain {\n"
         "    public let id: UUID\n"
         "    public var segments: [Segment]\n"
         "}\n\n"
         "extension CentipedeChain: Equatable {}\n"),
        ("Sources/CentipedeCore/HitResult.swift",
         "public enum HitResult {\n"
         "    case body(chainIdBefore: UUID, chainIdsAfter: [UUID])\n"
         "    case head(chainId: UUID)\n"
         "}\n"),
        ("Sources/CentipedeCore/ChainDirection.swift",
         "public enum ChainDirection { case left, right }\n"),
    ]
    out, notes = executor.normalize_boilerplate(files)
    out_map = dict(out)

    assert out_map["Sources/CentipedeCore/CentipedeChain.swift"].startswith(
        "import Foundation")
    assert out_map["Sources/CentipedeCore/HitResult.swift"].startswith(
        "import Foundation")
    # No Foundation symbol here — must be left exactly as it was.
    assert out_map["Sources/CentipedeCore/ChainDirection.swift"] == files[2][1]
    assert len(notes) == 2
