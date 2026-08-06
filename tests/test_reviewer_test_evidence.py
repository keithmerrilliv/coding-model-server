"""PASS requires a suite that exists — not one the reviewer personally wrote.

DEV-513's guard refused PASS when `result.test_files` was empty, reading "the
reviewer emitted no test files" as "the suite never ran". Those are different
propositions. When the implementer has already written the tests and the
reviewer correctly judges coverage complete, the original form converted a
correct judgement into a FAIL.

It fired twice in production (spec_346a029c and spec_1b927743), discarding a
verified-green run each time — the second on a reviewer that declined to add
tests citing the spec's own artifact-hygiene criterion, after the suite had
already run and passed 21/21.

The invariant is preserved: a workspace with no test file anywhere still
cannot report PASS (DEV-502 — a suite compiled by nothing goes vacuously
green).
"""
from coding_model_server.orchestrator_daemon import _workspace_has_test_files


def test_reviewer_supplied_tests_count():
    assert _workspace_has_test_files([], [("tests/test_x.py", "...")]) is True


def test_implementer_supplied_tests_count_even_when_the_reviewer_adds_none():
    """The regression this fixes."""
    code = [
        ("ElectricSheep/ContentView.swift", "..."),
        ("ElectricSheepTests/GenerationCancellationTests.swift", "@Test ..."),
    ]
    assert _workspace_has_test_files(code, []) is True


def test_no_tests_anywhere_still_refuses():
    """DEV-502: a suite that does not exist must never report PASS."""
    code = [("ElectricSheep/ContentView.swift", "..."), ("README.md", "...")]
    assert _workspace_has_test_files(code, []) is False


def test_recognises_each_dispatched_framework_convention():
    for path in [
        "ElectricSheepTests/GenerationCancellationTests.swift",  # xcodebuild
        "Tests/CentipedeCoreTests/GameStateTests.swift",         # swift_test
        "tests/test_scene.py",                                   # pytest
        "test_scene.py",                                         # pytest, flat
        "src/thing.test.ts",                                     # vitest/jest
        "src/thing.spec.js",
    ]:
        assert _workspace_has_test_files([(path, "")], []) is True, path


def test_does_not_mistake_ordinary_sources_for_tests():
    for path in [
        "ElectricSheep/ContentView.swift",
        "src/latest.py",
        "docs/testing.md",
        "Sources/Protest/Protest.swift",
    ]:
        assert _workspace_has_test_files([(path, "")], []) is False, path
