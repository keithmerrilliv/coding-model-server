# The change-surface parser skips table rows inside fenced code blocks (DEV-894)

## Context

Target repo: **coding-model-server** (self), `main` = `f0ca664`. Every fact
below was read from that commit.

`src/coding_model_autonomous/context.py` reads a spec's change-surface table
with three regexes, each anchored on a line that starts with `|`:
`_CHANGE_SURFACE_ROW` (used by `declared_modifications`), `_TABLE_PATH_ROW`
(used by `change_surface_path_rows`) and `_ANY_TABLE_ROW` (the `rows` count
in `change_surface`). None of them knows about fenced code blocks. A spec
that quotes a markdown table inside a ``` or ~~~ fence, to show an exact
edit, therefore *declares* the quoted rows: on 2026-09-28 the DEV-300 draft
quoted two README rows and its parsed surface gained `lib/lsystem.ts`, a
protected path the spec never touches. Planner rule 7a copies every
change-surface path into the plan's inputs and outputs, so a quoted row can
put an unrelated or protected file into the implement phase's outputs, with
no warning.

This change blanks every line of every fenced code block before those three
regexes run. Nothing else changes.

A reference implementation of exactly the edit and test file below ran on
Zooshly before this spec was written:

- the new test file: 10 passed with the edit; on unmodified `main`, T1, T2,
  T3, T5, T6 and T9 fail and T4, T7, T8, T10 pass (those four pin behaviour
  that is already right and must stay right);
- the whole suite with the edit: 3406 passed, 20 skipped; ruff and mypy clean;
- archive replay: all 180 specs (135 in `var/tasks_db/specs/*/spec.md`, 45 in
  `docs/specs/`) parse to the same `rows`, `declared`, `paths` and `kind`
  before and after. Three archived specs have a fenced line starting with
  `|`-like text, but each is an indented Swift `||` continuation that the
  column-0 anchors never matched.

Do not re-derive any of this.

## Authoritative design — reproduce this in your design document

All source edits are in `src/coding_model_autonomous/context.py`. That file
is large (about 1,340 lines): emit every change to it as anchored
SEARCH/REPLACE edits, **never** the whole file. The new test file is NEW:
emit it as one whole-file block (`<<<FILE: tests/test_change_surface_fences.py>>>`
… `END_FILE`), never as SEARCH/REPLACE.

1. Insert this block directly above the existing line
   `# A change-surface row whose second column marks the path as modified.`
   (that line is unique in the file and is the anchor), followed by one
   blank line between the block and that line:

````python
# A fenced code block (``` or ~~~, CommonMark's fence rules) quotes text: a
# table row inside one is an example the spec is showing, not a row it
# declares (DEV-894). The three table parsers below read the spec with every
# fenced line blanked; blanking rather than deleting keeps line numbers put.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _without_fenced_blocks(spec_md: str) -> str:
    """``spec_md`` with every line of every fenced code block made empty.

    A fence opens on a line indented at most three spaces that starts with
    three or more backticks or tildes (a backtick fence's info string may not
    contain a backtick). It closes on a line indented at most three spaces
    holding only the same character, at least as many times, and optional
    trailing whitespace. A fence that never closes runs to the end.
    """
    out: list[str] = []
    fence = ""
    for line in spec_md.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if not fence:
            m = _FENCE_OPEN.match(body)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                out.append(ending)
            else:
                out.append(line)
            continue
        stripped = body.lstrip(" ").rstrip()
        if (len(body) - len(body.lstrip(" ")) <= 3
                and len(stripped) >= len(fence)
                and stripped == fence[0] * len(stripped)):
            fence = ""
        out.append(ending)
    return "".join(out)
````

2. In `declared_modifications`, after the existing `if not spec_md:` /
   `return []` lines, add exactly one line:

````python
    spec_md = _without_fenced_blocks(spec_md)
````

3. In `change_surface_path_rows`, replace the line
   `    for m in _TABLE_PATH_ROW.finditer(spec_md):` with:

````python
    for m in _TABLE_PATH_ROW.finditer(_without_fenced_blocks(spec_md)):
````

4. In `change_surface`, replace the line
   `    return ChangeSurface(rows=len(_ANY_TABLE_ROW.findall(md)),` with:

````python
    return ChangeSurface(rows=len(_ANY_TABLE_ROW.findall(_without_fenced_blocks(md))),
````

   The two lines after it (`declared=…`, `paths=…`) stay exactly as they are;
   those helpers strip fences themselves.

No other line of `context.py` changes. The three regexes, `ChangeSurface`,
and the orchestrator daemon's aliases (`_declared_file_modifications`,
`_change_surface_path_rows`, `_change_surface`) are untouched.

5. The test file imports the code under test by its PACKAGE name. In the test
   sandbox the repository's `src/` directory is the package root on
   `sys.path`; it is NOT a package, so never import through `src.`.

## The new test file, verbatim

````python
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
````

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/context.py` | modify — add `_FENCE_OPEN` and `_without_fenced_blocks`; the three table parsers read the spec through it |
| `tests/test_change_surface_fences.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it: the daemon and the server)
- `src/coding_model_client/`
- `src/coding_model_autonomous/test_runner.py`
- `src/coding_model_autonomous/workspace.py`
- `src/coding_model_autonomous/outcome.py`
- `src/coding_model_autonomous/retry_policy.py`
- `src/coding_model_autonomous/executor.py`
- `src/coding_model_autonomous/db.py`
- `src/coding_model_autonomous/models.py`
- `src/coding_model_autonomous/test_strategy.py`
- `src/coding_model_autonomous/languages/`
- All existing tests.

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

T1–T10 are the ten tests in the file above, unchanged:

- **T1** — rows inside a ``` fence are neither declared modifications nor path rows.
- **T2** — the same for a ~~~ fence.
- **T3** — fenced rows are not counted in `ChangeSurface.rows`.
- **T4** — a table after a closed fence is still read.
- **T5** — a fence that never closes runs to the end of the spec.
- **T6** — a shorter fence, or the other fence character, does not close a fence.
- **T7** — a ``` line whose info string contains a backtick is not a fence.
- **T8** — ``` indented four spaces is not a fence.
- **T9** — a spec whose only table is fenced has `rows == 0` and `kind == "absent"`.
- **T10** — a spec with no fences parses exactly as before.

The whole existing suite stays green with no `--deselect`.

## Constraints

- No new dependencies. No DB schema changes. No daemon changes. No change to
  the three regexes or to `ChangeSurface`.
- The plan must carry the `repo` and `protected_paths` keys exactly as
  written below, so the existing file is fetched at `base_ref`.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_server/
  - src/coding_model_client/
  - src/coding_model_autonomous/test_runner.py
  - src/coding_model_autonomous/workspace.py
  - src/coding_model_autonomous/outcome.py
  - src/coding_model_autonomous/retry_policy.py
  - src/coding_model_autonomous/executor.py
  - src/coding_model_autonomous/db.py
  - src/coding_model_autonomous/models.py
  - src/coding_model_autonomous/test_strategy.py
  - src/coding_model_autonomous/languages/
```
