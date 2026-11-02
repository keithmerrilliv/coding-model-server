"""DEV-894: a table row inside a fenced code block is quoted, not declared."""
from coding_model_autonomous.context import (
    change_surface,
    change_surface_path_rows,
    declared_modifications,
)

TABLE = "| Path | Action |\n|---|---|\n| `src/a.py` | modify — the guard |\n| `tests/t.py` | new |\n"


def test_t1_backtick_fence_rows_are_not_declared():
    md = TABLE + "\n```text\n| `lib/x.ts` | modify — quoted |\n```\n"
    assert declared_modifications(md) == ["src/a.py"]
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t2_tilde_fence_rows_are_not_declared():
    md = TABLE + "\n~~~\n| `lib/x.ts` | modify |\n~~~\n"
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t3_fenced_rows_are_not_counted():
    md = TABLE + "\n```\n| A | B |\n|---|---|\n| `lib/x.ts` | modify |\n```\n"
    cs = change_surface(md)
    assert cs.rows == 3
    assert cs.declared == ["src/a.py"]
    assert cs.paths == ["src/a.py", "tests/t.py"]


def test_t4_table_after_fence_is_still_read():
    md = "```python\nx = 1\n```\n\n" + TABLE
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t5_unclosed_fence_runs_to_the_end():
    md = TABLE + "\n```\n| `lib/x.ts` | modify |\n"
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t6_shorter_or_other_char_line_does_not_close():
    md = "````\n```\n~~~\n| `lib/x.ts` | modify |\n````\n" + TABLE
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t7_backtick_in_info_string_is_not_a_fence():
    md = "```not `a` fence\n" + TABLE
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t8_fence_indented_four_spaces_is_not_a_fence():
    md = "    ```\n" + TABLE + "    ```\n"
    assert change_surface_path_rows(md) == ["src/a.py", "tests/t.py"]


def test_t9_only_fenced_table_is_absent():
    cs = change_surface("# Spec\n\n```\n" + TABLE + "```\n")
    assert cs.rows == 0
    assert cs.kind == "absent"


def test_t10_spec_without_fences_is_unchanged():
    cs = change_surface(TABLE)
    assert cs.rows == 3
    assert cs.declared == ["src/a.py"]
    assert cs.paths == ["src/a.py", "tests/t.py"]
