"""DEV-839 scope 1: the language packs, behind one interface.

The pipeline decides a file's language in one place (``languages``) and asks
the pack for everything language-specific. These pin the detector, the
opt-in design rules, the dispatch hooks, and the property the package exists
for: a new language is a new pack, and the daemon serves it unchanged.
"""
import ast
from pathlib import Path

import pytest
import yaml

from coding_model_autonomous import languages
from coding_model_autonomous.languages import (
    RULE_COMPLETENESS,
    RULE_EQUATABLE,
    RULE_SEAM_IMPORTS,
    LanguagePack,
)
from coding_model_autonomous.models import EventKind, SpecStatus

_SRC = Path(__file__).resolve().parent.parent / "src"


# ── the one detector ─────────────────────────────────────────────────────────

def test_packs_for_paths_is_by_extension_in_registry_order():
    names = [p.name for p in languages.packs_for_paths(
        ["web/app.ts", "Sources/A.swift", "LSystem.h", "tool.py", "README.md"])]
    assert names == ["swift", "python", "javascript", "c_family"]
    assert languages.packs_for_paths([]) == []
    assert languages.packs_for_paths(None) == []


def test_extension_case_does_not_matter():
    assert languages.pack_for_path("a/B.Swift").name == "swift"


def test_a_header_implies_a_pack_but_no_language():
    """`.h` could be C, C++ or Objective-C: it names the pack that knows the
    family, never a language on its own (DEV-781)."""
    assert languages.pack_for_path("Renderer.h").name == "c_family"
    assert languages.language_from_paths(["Renderer.h"]) is None
    assert languages.language_from_paths(
        ["Renderer.h", "Renderer.mm"]) == "objective-c++"


def test_prose_detection_ignores_markdown_and_metal():
    for text in ("README.md", "Shaders.metal", "design.md"):
        assert languages.packs_in_text(text) == []


# ── design rules opt in ──────────────────────────────────────────────────────

@pytest.mark.parametrize("files, rule, applies", [
    ("Sources/App/World.swift", RULE_COMPLETENESS, True),
    ("Sources/App/World.swift", RULE_SEAM_IMPORTS, False),
    ("src/pkg/world.py", RULE_SEAM_IMPORTS, True),
    ("src/pkg/world.py", RULE_EQUATABLE, False),
    # One module holds many types in TypeScript too: completeness would ask
    # for a file per type in types.ts (spec_d448e279).
    ("src/types.ts", RULE_COMPLETENESS, False),
    # Mixed: every language must opt in (DEV-831's Swift + header designs).
    ("World.swift LSystem.h", RULE_COMPLETENESS, False),
    # No known language: no language-specific rule.
    ("", RULE_EQUATABLE, False),
    ("notes.md", RULE_COMPLETENESS, False),
])
def test_rule_applies_only_when_every_language_opts_in(files, rule, applies):
    assert languages.rule_applies(files, rule) is applies


# ── the hooks ────────────────────────────────────────────────────────────────

def test_frameworks_map_to_their_packs():
    assert languages.pack_for_framework("swift_test").name == "swift"
    assert languages.pack_for_framework("XCODEBUILD_TEST").name == "swift"
    assert languages.pack_for_framework("pytest").name == "python"
    assert languages.pack_for_framework("vitest").name == "javascript"
    assert languages.pack_for_framework("none") is None
    assert languages.pack_for_framework(None) is None


def test_standing_rules_are_rendered_only_for_the_languages_present():
    assert languages.render_rules(["src/a.py", "web/b.ts"]) == ""
    assert languages.render_rules(["Sources/A.swift"]).startswith(
        "## Swift rules")


def test_target_rules_come_from_the_operator_test_strategy():
    rule = languages.render_target_rules(
        {"framework": "xcodebuild_test", "default_actor_isolation": "MainActor"})
    assert rule.startswith("## This target is default-isolated")
    assert languages.render_target_rules({}) == ""
    assert languages.render_target_rules(None) == ""


def test_only_a_pack_with_prechecks_returns_a_result():
    bad = [("Sources/A/X.swift",
            "final class X {\n    mutating func f() {}\n}\n")]
    swift = languages.pack_for_framework("swift_test")
    assert swift.run_prechecks(bad, [], {}).failed()
    assert languages.pack_for_framework("pytest").run_prechecks(bad, [], {}) is None


def test_declared_types_are_read_per_language():
    assert languages.declared_types("A.swift", "struct Field {}\n") == {"Field"}
    assert languages.declared_types("a.py", "class Field: pass\n") == set()


def test_imports_provide_symbols_only_in_their_own_language():
    """`import SwiftUI` provides `Button` to a Swift file. The same line in a
    Python file (a Swift fixture inside a string, as the archive has) provides
    nothing: Swift's import tables are Swift's."""
    swift_src = "import SwiftUI\nimport Metal\n"
    names, prefixes = languages.provided_symbols("App.swift", swift_src)
    assert "Button" in names and "sinf" in names and "MTL" in prefixes
    assert languages.provided_symbols("fixture.py", swift_src) == (frozenset(), ())


def test_value_mutability_is_opted_into_per_file():
    assert languages.path_opts_in("Sources/A/Model.swift",
                                  languages.RULE_VALUE_MUTABILITY)
    assert not languages.path_opts_in("LLab Shared/Generator/LSystem.h",
                                      languages.RULE_VALUE_MUTABILITY)


def test_normalize_file_applies_the_owning_pack_only():
    content, notes = languages.normalize_file(
        "package.json", '{"dependencies": {"a": "^1.2.3"}}')
    assert '"a": "1.2.3"' in content and len(notes) == 1
    content, notes = languages.normalize_file("A.swift", "let id = UUID()\n")
    assert content.startswith("import Foundation\n") and len(notes) == 1
    assert languages.normalize_file("a.py", "x = 1\n") == ("x = 1\n", [])


# ── a new language touches no daemon or kernel file ──────────────────────────

class _FakeViolation:
    path, line, message = "src/a.fake", 3, "fake: undefined name 'q'"


class _FakeResult:
    violations = [_FakeViolation()]

    def failed(self):
        return True

    def summary(self):
        return self.violations[0].message

    def report(self):
        return "src/a.fake:3:1: error: fake: undefined name 'q'\n"

    def event_payload(self):
        return [{"kind": "fake", "path": "src/a.fake", "line": 3}]


class _FakePack(LanguagePack):
    name = "fake"
    frameworks = frozenset({"fake_test"})
    design_rules = frozenset({RULE_COMPLETENESS})

    def standing_rules(self, paths):
        return "## Fake rules\n\n"

    def target_rules(self, test_strategy):
        return "## Fake target\n\n" if test_strategy.get("fake_mode") else ""

    def run_prechecks(self, files, context_files, test_strategy):
        return _FakeResult() if any(p.endswith(".fake") for p, _ in files) else None


@pytest.fixture
def fake_pack(monkeypatch):
    pack = _FakePack()
    monkeypatch.setitem(languages.PACKS, "fake", pack)
    monkeypatch.setitem(languages._PACK_OF_EXTENSION, ".fake", "fake")
    return pack


def test_a_registered_pack_is_served_by_the_shared_hooks(fake_pack):
    assert languages.pack_for_path("src/a.fake") is fake_pack
    assert languages.render_rules(["src/a.fake"]) == "## Fake rules\n\n"
    assert languages.render_target_rules({"fake_mode": True}) == "## Fake target\n\n"
    assert languages.pack_for_framework("fake_test") is fake_pack


def test_the_daemon_prechecks_a_registered_pack_unchanged(fake_pack, db):
    """The pre-dispatch check reaches a new language's prechecks through the
    framework alone: no daemon edit, and the same event a Swift hit records."""
    from coding_model_server import orchestrator_daemon as d

    spec = db.create_spec(title="fake", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    db.update_spec_status(spec.id, SpecStatus.EXECUTING, normalized_yaml=yaml.safe_dump(
        {"test_strategy": {"framework": "fake_test"}}))
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    spec, task = db.get_spec(spec.id), db.get_task(task.id)

    reason, report = d._local_precheck(
        db, spec, task, [("src/a.fake", "q\n")], [], "fake_test")
    assert reason == "fake: undefined name 'q'"
    assert report.startswith("src/a.fake:3:1: error:")
    (ev,) = db.list_events_by_kind(spec_id=spec.id, kind=EventKind.TEST_RAN)
    assert ev.payload["local_precheck"] is True
    assert ev.payload["precheck_violations"][0]["kind"] == "fake"

    # A framework whose pack has no prechecks dispatches as before.
    assert d._local_precheck(db, spec, task, [("a.py", "")], [], "pytest") == (None, "")


# ── the boundary ─────────────────────────────────────────────────────────────

def _imports_a_pack_module(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level and path.parent.name == "coding_model_autonomous":
                mod = "coding_model_autonomous." + mod
            if mod.startswith("coding_model_autonomous.languages."):
                return True
    return False


def test_nothing_outside_languages_reaches_into_a_pack():
    """Callers go through the interface, so a pack's modules can change
    without a caller noticing. The executor façade re-exports old names and
    is the one exception."""
    offenders = [
        str(p.relative_to(_SRC)) for p in sorted(_SRC.rglob("*.py"))
        if "languages" not in p.parts and p.name != "executor.py"
        and _imports_a_pack_module(p)]
    assert offenders == []


def test_the_boundary_check_sees_a_pack_import(tmp_path):
    """Negative control: the detector above does fire."""
    mod = tmp_path / "coding_model_autonomous" / "x.py"
    mod.parent.mkdir()
    mod.write_text("from .languages.swift import rules\n")
    assert _imports_a_pack_module(mod)
