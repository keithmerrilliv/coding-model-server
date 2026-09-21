"""DEV-781: Objective-C and Objective-C++ specs retrieve from the Apple corpus.

The corpus is Apple API documentation, which applies to every language that
calls those APIs. The gate compared the plan's free-text `language:` against
a set containing only `swift`; the planner's spelling ("objc", "Obj-C") and
a plan with no language at all both switched retrieval off silently.
"""
from types import SimpleNamespace

import pytest

from coding_model_autonomous import executor as ex


@pytest.mark.parametrize("raw, want", [
    ("objc", "objective-c"), ("Obj-C", "objective-c"), ("Objective-C", "objective-c"),
    ("ObjectiveC", "objective-c"), ("objective c", "objective-c"),
    ("objc++", "objective-c++"), ("Obj-C++", "objective-c++"), ("objcpp", "objective-c++"),
    ("Objective-C++", "objective-c++"), ("Swift", "swift"), ("PY", "python"),
    ("rust", "rust"), ("", None), (None, None), ("  ", None),
])
def test_normalise_every_spelling(raw, want):
    assert ex.normalize_language(raw) == want


def test_default_coverage_is_the_apple_family():
    assert {"swift", "objective-c", "objective-c++"} <= ex.AUTONOMOUS_MEMORY_LANGUAGES


def test_gate_retrieves_on_every_spelling_and_still_refuses_python(monkeypatch):
    monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", {"architect"})
    monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_LANGUAGES", {"swift", "objective-c", "objective-c++"})
    for lang in ("Obj-C", "objc", "Objective-C++", "objcpp", "swift"):
        assert ex.retrieval_decision("architect", lang) == (True, "retrieved"), lang
    assert ex.retrieval_decision("architect", "python") == (False, "language_not_covered")
    assert ex.retrieval_decision("architect", None) == (False, "language_unknown")


@pytest.mark.parametrize("paths, want", [
    (["Sources/Bridge.mm", "Sources/Bridge.h"], "objective-c++"),
    (["JSONParser/Parser.m", "JSONParser/Parser.h"], "objective-c"),
    (["ElectricSheep/AudioManager.swift"], "swift"),
    (["Sources/A.swift", "Sources/Legacy.m"], "objective-c"),      # the more specific claim wins
    (["Sources/A.h"], None),                                        # a header alone decides nothing
    ([], None), (None, None),
    (["src/pkg/mod.py"], "python"),
])
def test_language_from_paths(paths, want):
    assert ex.language_from_paths(paths) == want


def _spec(plan_yaml):
    return SimpleNamespace(id="spec_t", normalized_yaml=plan_yaml)


def test_spec_language_falls_back_to_the_implement_outputs():
    import coding_model_server.orchestrator_daemon as d
    no_lang = ("phases:\n  - name: implement\n    outputs:\n      - Sources/Bridge.mm\n"
               "      - Sources/Bridge.h\n")
    assert d._spec_language(_spec(no_lang)) == "objective-c++"
    declared = "language: Obj-C\n" + no_lang
    assert d._spec_language(_spec(declared)) == "objective-c"      # the plan wins, normalised
    assert d._spec_language(_spec("language: swift\n")) == "swift"
    assert d._spec_language(_spec(None)) is None
    assert d._spec_language(_spec("phases:\n  - name: implement\n    outputs:\n      - README.md\n")) is None
