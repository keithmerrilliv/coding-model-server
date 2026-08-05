"""A corrupted directory in a manifest path must not silently no-op — DEV-500.

On run 5 of the Centipede spec (spec_9872c963, 2026-08-04 16:45) the implementer
emitted `Tests/CentipegeCoreTests/CoreLogicTests.swift` — one character wrong —
after writing the correct `Tests/CentipedeCoreTests/` on both previous attempts.

That is worse than a build failure. The five source files still land correctly
so the module compiles; SwiftPM claims only `Tests/CentipedeCoreTests`, so the
test file belongs to no target and is never built; `swift test` runs the
pre-existing suite, which passes. The pre-gate check (DEV-429) then reports
"compiled and the suite passed" for a spec that delivered none of its 17
acceptance criteria, and every downstream signal agrees, because the only thing
that would disagree does not exist.

The same run corrupted `CentipedeCore` → `CentidepeCore` in a design and
`<<<DESIGN>>>` → `<<<DESINVARIANT>>>` three times (DEV-498). Long identifiers
lose a character or two at a noticeable rate; only some of it is loud.
"""
from types import SimpleNamespace

import pytest

import coding_model_server.orchestrator_daemon as d

# The design lays its files out as a tree — `Tests/` on one line, then
# `└── CentipedeCoreTests/` — so no full path prefix ever appears contiguously.
DESIGN = """\
## File Structure
```
Sources/
└── CentipedeCore/
    ├── GameState.swift          (PROTECTED)
    ├── Types.swift              NEW
    └── GameWorld.swift          NEW
Tests/
└── CentipedeCoreTests/
    ├── GameStateTests.swift     (PROTECTED)
    └── CoreLogicTests.swift     NEW
```
"""


def _entries(*paths):
    return [SimpleNamespace(path=p) for p in paths]


def _repair(entries, design=DESIGN):
    # record_event is called with a positional EventKind first
    db = SimpleNamespace(record_event=lambda *a, **kw: None)
    spec = SimpleNamespace(id="spec_test")
    task = SimpleNamespace(id="task_test")
    return d._repair_manifest_dirs(db, spec, task, entries, design)


# ── extracting what the design declares ─────────────────────────────────────

def test_tree_layout_directories_are_found():
    """Prefix matching finds nothing here — the whole reason this is
    component-based."""
    known = d._design_dir_components(DESIGN)
    assert {"Sources", "CentipedeCore", "Tests", "CentipedeCoreTests"} <= known


# ── the two corruption shapes seen in production ────────────────────────────

def test_the_live_substitution_is_repaired():
    """The regression: CentipedeCoreTests -> CentipegeCoreTests."""
    e = _entries("Tests/CentipegeCoreTests/CoreLogicTests.swift")
    assert _repair(e) == 1
    assert e[0].path == "Tests/CentipedeCoreTests/CoreLogicTests.swift"


def test_a_transposition_is_repaired():
    """CentipedeCore -> CentidepeCore, seen in a design on the same run. Two
    substitutions, so it needs its own case."""
    e = _entries("Sources/CentidepeCore/MushroomField.swift")
    assert _repair(e) == 1
    assert e[0].path == "Sources/CentipedeCore/MushroomField.swift"


def test_correct_paths_are_untouched():
    e = _entries("Sources/CentipedeCore/Types.swift",
                 "Tests/CentipedeCoreTests/CoreLogicTests.swift")
    assert _repair(e) == 0
    assert e[0].path == "Sources/CentipedeCore/Types.swift"
    assert e[1].path == "Tests/CentipedeCoreTests/CoreLogicTests.swift"


# ── it must not invent corrections ──────────────────────────────────────────

def test_a_genuinely_new_directory_is_left_alone():
    """A new directory differs by whole words, not one letter. Rewriting it
    would be worse than the typo."""
    e = _entries("Sources/Rendering/Metal4Renderer.swift")
    assert _repair(e) == 0
    assert e[0].path == "Sources/Rendering/Metal4Renderer.swift"


def test_an_ambiguous_near_miss_is_left_alone():
    """Two candidates one edit away means we cannot know which was meant."""
    design = "```\nSources/\n└── Foo/\n└── Fooo/\n└── Fooa/\n```"
    assert d._closest_component("Foob", d._design_dir_components(design)) is None


def test_the_filename_is_never_repaired():
    """Only directories are corrected — a filename is the design's business."""
    e = _entries("Sources/CentipedeCore/Typos.swift")
    assert _repair(e) == 0
    assert e[0].path.endswith("Typos.swift")


def test_a_design_with_no_directories_is_a_no_op():
    e = _entries("Tests/CentipegeCoreTests/CoreLogicTests.swift")
    assert _repair(e, design="prose only, no paths") == 0
    assert e[0].path == "Tests/CentipegeCoreTests/CoreLogicTests.swift"


def test_a_bare_filename_is_ignored():
    e = _entries("Package.swift")
    assert _repair(e) == 0


# ── multiple components, and multiple entries ───────────────────────────────

def test_every_corrupted_component_in_one_path_is_repaired():
    e = _entries("Sourses/CentipegeCoreTests/X.swift")
    design = DESIGN + "\n```\nSourses_placeholder\n```"
    _repair(e, DESIGN)
    assert e[0].path == "Sources/CentipedeCoreTests/X.swift"


def test_repairs_are_counted_across_entries():
    e = _entries("Tests/CentipegeCoreTests/A.swift",
                 "Sources/CentidepeCore/B.swift",
                 "Sources/CentipedeCore/C.swift")
    assert _repair(e) == 2


@pytest.mark.parametrize("bad,good", [
    ("CentipegeCoreTests", "CentipedeCoreTests"),
    ("CentidepeCore", "CentipedeCore"),   # swap two apart, not adjacent
    ("Sourcse", "Sources"),
])
def test_known_corruption_shapes(bad, good):
    assert d._closest_component(bad, d._design_dir_components(DESIGN)) == good
