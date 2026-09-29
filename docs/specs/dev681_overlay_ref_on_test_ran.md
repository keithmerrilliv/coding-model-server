# TEST_RAN records the overlay ref the self-target sandbox was built from (DEV-681)

## Context

Target repo: **coding-model-server** (self). DEV-654 made the self-target
sandbox overlay the COMMITTED tree (`git archive HEAD src`), and
`_materialize_local_repo_overlay` already computes the short HEAD sha and the
uncommitted paths under `src/` (`_src_tree_state`) — but only to log one
journal line. Which HEAD a dogfood run's tests were judged against, and
whether the tree was clean, is the evidence the whole DEV-628 epic rests on,
and today it is a journal grep that rotates away.

The same pattern that carries the DEV-536 reconstruction marker and the
DEV-675 existing-tests header carries this: the overlay builder writes a small
JSON note into the overlay directory, the test runner turns it into one
header line of the test output, and the daemon parses that line into the
`pre_gate_build_check` TEST_RAN payload.

Two files change, both large (`test_runner.py` is about 1,780 lines and
`orchestrator_daemon.py` about 6,900). **Emit every edit to them as an
anchored SEARCH/REPLACE block, never as a whole file.** Each edit below names
its anchor, and every anchor is unique in its file.

## Authoritative design — reproduce this in your design document

### `src/coding_model_autonomous/test_runner.py`

1. **`import json`.** The module does not import it today. Replace the three
lines

```python
import logging
import os
import re
```

with

```python
import json
import logging
import os
import re
```

2. **The marker and the regex.** Immediately after the existing line
`EXISTING_TESTS_MARKER = "[self-target existing tests]"` (unique, the
anchor), add, preceded by a two-line comment naming DEV-681:

```python
OVERLAY_REF_MARKER = "[repo overlay]"
OVERLAY_REF_FILE = "OVERLAY_REF.json"
_OVERLAY_REF_RE = re.compile(
    re.escape(OVERLAY_REF_MARKER)
    + r" source=(?P<source>\w+) head=(?P<head>\S+) dirty=(?P<dirty>\d+)")
```

3. **The dataclass and three functions.** Immediately BEFORE the line
`def parse_test_split(output: str) -> Optional[TestSplit]:` (unique, the
anchor), insert this block, in this order, with two blank lines between
top-level definitions:

```python
@dataclass
class OverlayRef:
    """Which repository state a self-target sandbox overlay was built from (DEV-681)."""
    source: str          # committed | working_tree | fallback
    head: str            # short sha, or "unknown"
    dirty: int           # uncommitted paths under src/ the sandbox did not see

    def header(self) -> str:
        return f"{OVERLAY_REF_MARKER} source={self.source} head={self.head} dirty={self.dirty}"

    def payload(self) -> dict:
        return {"overlay_source": self.source, "overlay_head": self.head,
                "overlay_dirty": self.dirty}
```

- `note_overlay_ref(overlay_root: Path, source: str, head: str, dirty: list[str]) -> None`
  creates `overlay_root` with `mkdir(parents=True, exist_ok=True)`, then
  writes `overlay_root / OVERLAY_REF_FILE` as
  `json.dumps({"source": source, "head": head, "dirty": dirty}, indent=2)`.
- `read_overlay_ref(overlay_root: Path) -> Optional[OverlayRef]` reads that
  file and returns `OverlayRef(source, head, len(dirty))`. It returns None
  when the file is missing or unparseable (`OSError`, `ValueError`,
  `KeyError`, `TypeError`).
- `parse_overlay_ref(output: str) -> Optional[OverlayRef]` searches
  `output or ""` with `_OVERLAY_REF_RE` and returns
  `OverlayRef(source, head, int(dirty))`, or None when there is no header.

4. **The three branch notes in `_materialize_local_repo_overlay`.** Use
`overlay_src.parent` as the overlay root (it is `spec_dir / _REPO_OVERLAY_DIR`).
The function's return value and everything else in it are unchanged. There
are three edits, each with its own anchor:

- Working-tree branch. Anchor: the line
  `"sandbox sees uncommitted edits (DEV-654)")` (unique). After that line,
  at the same indent as the `logger.warning(` above it, add:

```python
        try:
            sha, dirty = _src_tree_state(_SERVER_REPO_ROOT)
        except Exception:
            sha, dirty = "unknown", []
        note_overlay_ref(overlay_src.parent, "working_tree", sha, dirty)
```

- Fallback branch. `shutil.copytree(repo_src, overlay_src, …)` appears twice
  in the function, so anchor on this five-line block, which is unique
  because of the `else:` that ends it:

```python
            if overlay_src.exists():
                shutil.rmtree(overlay_src)
            shutil.copytree(repo_src, overlay_src,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
```

  Keep those five lines and add, between the `copytree(…)` statement and
  the `else:`, at the same indent as `shutil.copytree`:

```python
            note_overlay_ref(overlay_src.parent, "fallback", "unknown", [])
```

- Committed branch. Anchor: the line
  `logger.info("repo overlay built from HEAD %s (working tree clean)", sha)`
  (unique). After it, dedented to the indent of the `if dirty:` above it
  (12 spaces), add:

```python
            note_overlay_ref(overlay_src.parent, "committed", sha, dirty)
```

5. **The header in `_run_local_tests`.** Three edits:

- Immediately after the line `    existing_header = ""` (unique, 4-space
  indent), add `    overlay_header = ""`.
- Immediately after the line
  `            extra_env = {"PYTHONPATH": str(overlay_src)}` (unique), and
  BEFORE the `mode, selected = select_existing_tests(...)` line that
  follows it, add:

```python
            # Read now: select_existing_tests clears everything but src/
            # under the overlay root, this note included (DEV-681).
            ref = read_overlay_ref(overlay_src.parent)
            if ref is not None:
                overlay_header = ref.header()
```

- The pytest branch ends today with these three lines (the anchor):

```python
    if existing_header:
        output = existing_header + "\n" + output
    return passed, output
```

  Replace them with:

```python
    header = "\n".join(h for h in (existing_header, overlay_header) if h)
    if header:
        output = header + "\n" + output
    return passed, output
```

  The output is then: the existing-tests header line FIRST, then the overlay
  header line, then the confined run's output, each header on its own line.
  Every self-target run carries an existing-tests header (even
  `mode=off selected=0: none`), so the overlay header is always the SECOND
  line of a self-target run's output, never the first.

  This puts a line between the existing-tests header and the run output,
  which one existing assertion reads (edit 8 below).

### `src/coding_model_server/orchestrator_daemon.py`

6. The DEV-675 block now lives in `_pre_gate_build_check`, inside an `if`,
at 12-space indent. Anchor (unique) on these three lines:

```python
            test_split = test_runner.parse_test_split(build_output)
            if test_split is not None:
                build_payload.update(test_split.payload())
```

Keep them and add, immediately after, at the same 12-space indent:

```python
            # DEV-681: which repository state the overlay was built from, on the
            # event a later query reads — not only in a journal line.
            overlay_ref = test_runner.parse_overlay_ref(build_output)
            if overlay_ref is not None:
                build_payload.update(overlay_ref.payload())
```

Nothing else in the daemon changes.

### `tests/test_existing_tests_selection.py`

7. One existing assertion encodes the old layout, where the run output
follows the header directly. It is in
`test_selected_existing_tests_are_on_the_pytest_command_line`. Anchor
(unique) on this line:

```python
    assert rest.startswith("tests/test_new.py::test_new PASSED")
```

Replace it with these three lines, at the same 4-space indent:

```python
    overlay, _, body = rest.partition("\n")
    assert overlay.startswith("[repo overlay] source=committed")
    assert body.startswith("tests/test_new.py::test_new PASSED")
```

Change nothing else in that file. It is the ONLY existing test edited.

### The new test file

8. The test file imports the code under test by its PACKAGE name, and the
design's Criterion Seams must quote this line verbatim as shared setup:

```python
from coding_model_autonomous import test_runner as tr
```

In the test sandbox the repository's `src/` directory is the package root
on `sys.path`. It is NOT a package, so `from src.coding_model_autonomous…`
fails at collection with `ModuleNotFoundError`. Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/test_runner.py` | modify — `import json`, markers, `OverlayRef`, `note_overlay_ref`/`read_overlay_ref`/`parse_overlay_ref`, the three branch notes, the header in `_run_local_tests` |
| `src/coding_model_server/orchestrator_daemon.py` | modify — three statements after the DEV-675 block in `_pre_gate_build_check` |
| `tests/test_existing_tests_selection.py` | modify — one assertion, edit 7 |
| `tests/test_overlay_ref_on_test_ran.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_autonomous/models.py`
- `src/coding_model_autonomous/outcome.py`
- `src/coding_model_autonomous/workspace.py`
- `src/coding_model_autonomous/executor.py`
- `src/coding_model_autonomous/context.py`
- `src/coding_model_autonomous/retry_policy.py`
- `src/coding_model_autonomous/db.py`
- `src/coding_model_autonomous/languages/`
- `src/coding_model_client/`
- All existing tests, except the one assertion edit 7 names.

## Test scaffolding

The test file reuses the shape of `tests/test_overlay_from_committed_ref.py`:
a `_git(root, *args)` helper running `git -C root -c user.name=t -c user.email=t@t …`
with `check=True, capture_output=True`, and a `repo` fixture that creates
`tmp_path / "checkout"` with `src/pkg/mod.py` = `"A\n"`, runs `git init -q`,
`git add .`, `git commit -q -m base`, monkeypatches `tr._SERVER_REPO_ROOT` to
that root, deletes `AUTONOMOUS_OVERLAY_FROM_WORKING_TREE` from the
environment, and returns `(root, spec_dir)` with `spec_dir = tmp_path / "spec"`
created. `head(root)` returns `git -C root rev-parse --short HEAD` stripped.

**There is no `spec_dir` fixture.** A test that needs the spec directory takes
the `repo` fixture and unpacks it: `root, spec_dir = repo`. T1–T5 need neither
(`tmp_path` at most). T9 builds its own non-git directory under `tmp_path` and
its own `spec_dir = tmp_path / "spec"`. A test that names `spec_dir` as a
parameter fails at setup with `fixture 'spec_dir' not found`, which is how the
09-29 run lost all five of those tests.

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

- **T1** — `tr.OverlayRef("committed", "abc1234", 0).header() == "[repo overlay] source=committed head=abc1234 dirty=0"` and its `payload() == {"overlay_source": "committed", "overlay_head": "abc1234", "overlay_dirty": 0}`.
- **T2** — `tr.parse_overlay_ref("[self-target existing tests] mode=imports selected=1: tests/test_x.py\n[repo overlay] source=committed head=abc1234 dirty=2\n1 passed\n") == tr.OverlayRef("committed", "abc1234", 2)`.
- **T3** — `tr.parse_overlay_ref("1 passed in 0.01s\n") is None` and `tr.parse_overlay_ref("") is None`.
- **T4** — `tr.note_overlay_ref(tmp_path / "ov", "committed", "abc1234", ["src/pkg/mod.py"])` writes `tmp_path / "ov" / "OVERLAY_REF.json"` whose JSON is `{"source": "committed", "head": "abc1234", "dirty": ["src/pkg/mod.py"]}`, and `tr.read_overlay_ref(tmp_path / "ov") == tr.OverlayRef("committed", "abc1234", 1)`.
- **T5** — `tr.read_overlay_ref(tmp_path / "nowhere") is None`; after writing `"not json"` to `tmp_path / "bad" / "OVERLAY_REF.json"`, `tr.read_overlay_ref(tmp_path / "bad") is None`.
- **T6** — with the `repo` fixture and a clean tree, `tr._materialize_local_repo_overlay(spec_dir, root.name)` is not None and `tr.read_overlay_ref(spec_dir / tr._REPO_OVERLAY_DIR) == tr.OverlayRef("committed", head(root), 0)`.
- **T7** — as T6 but after writing `"B\n"` to `root / "src" / "pkg" / "mod.py"` without committing: the ref is `tr.OverlayRef("committed", head(root), 1)` and the JSON file's `dirty` list equals `["src/pkg/mod.py"]`.
- **T8** — as T6 with `monkeypatch.setenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", "1")`: the ref's `source == "working_tree"` and `head == head(root)`.
- **T9** — with `tr._SERVER_REPO_ROOT` monkeypatched to a directory that has `src/pkg/mod.py` but is NOT a git checkout, `tr._materialize_local_repo_overlay(spec_dir, root.name)` is not None and the ref is `tr.OverlayRef("fallback", "unknown", 0)`.
- **T10** — `_run_local_tests` puts the header on the output: with the `repo` fixture, `monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "off")`, `spec_dir / "src" / "pkg"` created holding `mod.py` = `"B\n"`, `spec_dir / "tests" / "test_new.py"` = `"def test_new():\n    assert True\n"`, and `tr._run_confined` replaced (via `mock.patch.object`) by a function `fake(raw_cmd, spec_dir, timeout, *, what, share_net=False, extra_binds=None, extra_env=None)` returning `(True, "1 passed in 0.01s\n")`: `passed, output = tr._run_local_tests(spec_dir, "pytest", 60, repo=root.name)` gives `passed is True`, `output.splitlines()[0]` starts with `"[self-target existing tests] mode=off"`, `output.splitlines()[1] == f"[repo overlay] source=committed head={head(root)} dirty=0"`, and `tr.parse_overlay_ref(output).payload()["overlay_head"] == head(root)`.
- **T11** — as T10 but with the mode env deleted (`imports`) and a committed `tests/test_mod.py` = `"from pkg.mod import X\n"` in the checkout (add it and commit it in the test before calling): `output.splitlines()[0]` starts with `"[self-target existing tests] mode=imports"` and `output.splitlines()[1]` starts with `"[repo overlay] source=committed"`.

## Constraints

- No new dependencies. `_src_tree_state`, `_extract_committed_src`,
  `parse_test_split`, `TestSplit` and `existing_tests_header` are unchanged.
- The plan must carry the `repo` and `protected_paths` keys exactly as written
  below, so the existing files are fetched at `base_ref`.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_autonomous/models.py
  - src/coding_model_autonomous/outcome.py
  - src/coding_model_autonomous/workspace.py
  - src/coding_model_autonomous/executor.py
  - src/coding_model_autonomous/context.py
  - src/coding_model_autonomous/retry_policy.py
  - src/coding_model_autonomous/db.py
  - src/coding_model_autonomous/languages/
  - src/coding_model_client/
```
