"""Test async def test_… counting for pytest (DEV-911).

The Python language pack's _count_pytest_tests must accept both
def test_…( and async def test_…( declarations.
"""
from __future__ import annotations

from coding_model_autonomous.test_runner import count_test_declarations, declaration_delta
from coding_model_autonomous.languages import pack_for_framework


# ── T1 ───────────────────────────────────────────────────────────────────────

def test_t1_async_def_single() -> None:
    """T1 — count_test_declarations("async def test_x():\n    pass\n", "pytest") == 1."""
    assert count_test_declarations("async def test_x():\n    pass\n", "pytest") == 1


# ── T2 ───────────────────────────────────────────────────────────────────────

def test_t2_mixed_top_level_and_indented() -> None:
    """T2 — mixed top-level def, async def, and indented async method returns 3."""
    source = (
        "def test_a():\n"
        "    pass\n"
        "\n"
        "async def test_b():\n"
        "    pass\n"
        "\n"
        "class TestC:\n"
        "    async def test_c(self):\n"
        "        pass\n"
    )
    assert count_test_declarations(source, "pytest") == 3


# ── T3 ───────────────────────────────────────────────────────────────────────

def test_t3_commented_out_does_not_count() -> None:
    """T3 — commented-out "# async def test_old()" does not count; only real def counts → 1."""
    source = (
        "# async def test_old():\n"
        "def test_new():\n"
        "    pass\n"
    )
    assert count_test_declarations(source, "pytest") == 1


# ── T4 ───────────────────────────────────────────────────────────────────────

def test_t4_docstring_does_not_count() -> None:
    """T4 — declaration inside triple-quoted docstring does not count; only real def counts → 1."""
    source = (
        '"""\n'
        'async def test_in_docstring():\n'
        '"""\n'
        "def test_real():\n"
        "    pass\n"
    )
    assert count_test_declarations(source, "pytest") == 1


# ── T5 ───────────────────────────────────────────────────────────────────────

def test_t5_non_test_names_do_not_count() -> None:
    """T5 — non-test-name async defs (helper, fixture_*) return 0."""
    source = (
        "async def helper():\n"
        "    pass\n"
        "\n"
        "async def fixture_test_x():\n"
        "    pass\n"
    )
    assert count_test_declarations(source, "pytest") == 0


# ── T6 ───────────────────────────────────────────────────────────────────────

def test_t6_declaration_delta_adds_async() -> None:
    """T6 — declaration_delta detects +1 new async test in t.py when added alongside existing sync test."""
    before = {"t.py": "def test_a():\n    pass\n"}
    after = {
        "t.py": (
            "def test_a():\n"
            "    pass\n"
            "\n"
            "async def test_b():\n"
            "    pass\n"
        )
    }
    assert declaration_delta(before, after, "pytest") == {"t.py": 1}


# ── T7 ───────────────────────────────────────────────────────────────────────

def test_t7_pack_counts_async_directly() -> None:
    """T7 — pack_for_framework("pytest").count_tests on async source returns 1 directly through the PythonPack."""
    pack = pack_for_framework("pytest")
    assert pack is not None
    assert pack.count_tests("async def test_x():\n    pass\n") == 1


# ── T8 ───────────────────────────────────────────────────────────────────────

def test_t8_plain_def_still_works() -> None:
    """T8 — plain "def test_a()" still counts as 1 — backward compatibility unchanged."""
    assert count_test_declarations("def test_a():\n    pass\n", "pytest") == 1
