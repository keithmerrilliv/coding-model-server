"""DEV-407 — vacuous assertions in generated tests must be surfaced.

spec_96d7e07f's synthesized tests contained `assert.ok(x || true)`, so its
15/17 pass count overstated coverage. scan_tautological_asserts gives the
reviewer line-precise facts via the same deterministic-scan pattern as the
`any`-type section — an instruction, never a hard failure.
"""
from coding_model_autonomous.executor import (
    build_reviewer_message,
    scan_tautological_asserts,
)


def _hits(path, content):
    return scan_tautological_asserts([(path, content)])


def test_detects_the_dogfood_pattern():
    hits = _hits("game.test.js",
                 "assert.ok(updatedCentipedes.length > 0 || true, 'msg');\n")
    assert len(hits) == 1
    assert hits[0].startswith("game.test.js:1")


def test_detects_literal_true_and_expect_identity():
    content = (
        "assert.ok(true);\n"
        "expect(true).toBe(true);\n"
        "assert.ok(centipedes.length > 0);\n"
    )
    hits = _hits("tests/logic.spec.js", content)
    assert len(hits) == 2, "the real assertion on line 3 must not match"


def test_python_variants():
    content = "assert True\nself.assertTrue(True)\nassert x == 1\n"
    assert len(_hits("tests/test_x.py", content)) == 2


def test_implementation_files_are_not_scanned():
    """`x or True` in implementation code is ordinary logic."""
    assert _hits("core.js", "if (ready || true) { start(); } // gated\n") == []


def test_reviewer_message_carries_the_section():
    messages = build_reviewer_message(
        "# spec", "# design",
        [("core.js", "export const x = 1;\n"),
         ("game.test.js", "assert.ok(x || true);\n")],
        test_framework="node_test",
    )
    body = messages[-1]["content"]
    assert "Tautological assertions detected" in body
    assert "game.test.js:1" in body
    assert "NOT grounds for a FAIL verdict" in body


def test_no_section_when_clean():
    messages = build_reviewer_message(
        "# spec", "# design",
        [("game.test.js", "assert.strictEqual(f(), 42);\n")],
        test_framework="node_test",
    )
    assert "Tautological assertions" not in messages[-1]["content"]
