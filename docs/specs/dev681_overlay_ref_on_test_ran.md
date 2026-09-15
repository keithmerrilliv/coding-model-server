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

## Authoritative design — reproduce this in your design document

### `src/coding_model_autonomous/test_runner.py`

1. Immediately after the existing line
   `EXISTING_TESTS_MARKER = "[self-target existing tests]"`, add:
   ```python
   OVERLAY_REF_MARKER = "[repo overlay]"
   OVERLAY_REF_FILE = "OVERLAY_REF.json"
   _OVERLAY_REF_RE = re.compile(
       re.escape(OVERLAY_REF_MARKER) + r" source=(?P<source>\w+) head=(?P<head>\S+) dirty=(?P<dirty>\d+)")
   ```
   preceded by a two-line comment naming DEV-681.

2. Immediately after the existing `TestSplit` dataclass (and before
   `parse_test_split`), add a dataclass:
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

3. Immediately after `parse_test_split`, add three module-level functions:
   - `note_overlay_ref(overlay_root: Path, source: str, head: str, dirty: list[str]) -> None`
     — writes `overlay_root / OVERLAY_REF_FILE` as JSON
     `{"source": source, "head": head, "dirty": dirty}` (`json.dumps(..., indent=2)`;
     create `overlay_root` with `mkdir(parents=True, exist_ok=True)` first).
   - `read_overlay_ref(overlay_root: Path) -> Optional[OverlayRef]` — reads
     that file and returns `OverlayRef(source, head, len(dirty))`; returns
     None when the file is missing or unparseable (`OSError`, `ValueError`,
     `KeyError`, `TypeError`).
   - `parse_overlay_ref(output: str) -> Optional[OverlayRef]` — searches
     `output or ""` with `_OVERLAY_REF_RE` and returns
     `OverlayRef(source, head, int(dirty))`, or None when there is no header.

4. In `_materialize_local_repo_overlay`, record the ref on every branch,
   using `overlay_src.parent` as the overlay root (that is
   `spec_dir / _REPO_OVERLAY_DIR`):
   - the `AUTONOMOUS_OVERLAY_FROM_WORKING_TREE=1` branch: after its existing
     `logger.warning(...)`, compute `sha, dirty = _src_tree_state(_SERVER_REPO_ROOT)`
     inside a `try` (on `Exception` use `"unknown", []`) and call
     `note_overlay_ref(overlay_src.parent, "working_tree", sha, dirty)`.
   - the `except Exception as exc:` fallback branch: after its existing
     `shutil.copytree(...)`, call
     `note_overlay_ref(overlay_src.parent, "fallback", "unknown", [])`.
   - the `else:` (committed) branch: after the existing `sha, dirty = _src_tree_state(_SERVER_REPO_ROOT)`
     and its if/else logging, call
     `note_overlay_ref(overlay_src.parent, "committed", sha, dirty)`.
   The function's return value and everything else in it are unchanged.

5. In `_run_local_tests`, the pytest branch ends today with:
   ```python
   passed, output = _run_confined(raw_cmd, spec_dir, timeout, what="tests",
                                  extra_env=extra_env)
   if existing_header:
       output = existing_header + "\n" + output
   return passed, output
   ```
   Change it so the overlay header follows the existing-tests header. Before
   the `_run_confined` call, initialise `overlay_header = ""`; inside the
   `if overlay_src is not None:` block (after `extra_env = {"PYTHONPATH": ...}`)
   set
   ```python
   ref = read_overlay_ref(overlay_src.parent)
   if ref is not None:
       overlay_header = ref.header()
   ```
   and replace the tail with:
   ```python
   if existing_header:
       output = existing_header + "\n" + output
   if overlay_header:
       output = (existing_header + "\n" if existing_header else "") + overlay_header + "\n" + \
           output[len(existing_header) + 1:] if existing_header else overlay_header + "\n" + output
   return passed, output
   ```
   Simplify that however you like, provided the resulting output is:
   the existing-tests header line (when present) FIRST, then the overlay
   header line, then the confined run's output — each header on its own line.
   The existing test `tests/test_existing_tests_selection.py` asserts the
   existing-tests header is the first line; it must keep passing.

### `src/coding_model_server/orchestrator_daemon.py`

6. In `_run_implementer`, immediately after the DEV-675 block
   ```python
   test_split = test_runner.parse_test_split(build_output)
   if test_split is not None:
       build_payload.update(test_split.payload())
   ```
   add
   ```python
   # DEV-681: which repository state the overlay was built from, on the
   # event a later query reads — not only in a journal line.
   overlay_ref = test_runner.parse_overlay_ref(build_output)
   if overlay_ref is not None:
       build_payload.update(overlay_ref.payload())
   ```
   Nothing else in the daemon changes.

7. The test file imports the code under test by its PACKAGE name and the
   design's Criterion Seams quote the import line as shared setup:
   `from coding_model_autonomous import test_runner as tr`. In the test
   sandbox the repository's `src/` directory is the package root on
   `sys.path`; it is NOT a package, so `from src.coding_model_autonomous…`
   fails at collection with `ModuleNotFoundError`. Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/test_runner.py` | modify — markers, `OverlayRef`, `note_overlay_ref`/`read_overlay_ref`/`parse_overlay_ref`, the three branch notes, the header in `_run_local_tests` |
| `src/coding_model_server/orchestrator_daemon.py` | modify — three lines after the DEV-675 block in `_run_implementer` |
| `tests/test_overlay_ref_on_test_ran.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_autonomous/models.py`
- `src/coding_model_autonomous/outcome.py`
- `src/coding_model_autonomous/workspace.py`
- `src/coding_model_autonomous/executor.py`
- All existing tests.

## Test scaffolding

The test file reuses the shape of `tests/test_overlay_from_committed_ref.py`:
a `_git(root, *args)` helper running `git -C root -c user.name=t -c user.email=t@t …`
with `check=True, capture_output=True`, and a `repo` fixture that creates
`tmp_path / "checkout"` with `src/pkg/mod.py` = `"A\n"`, runs `git init -q`,
`git add .`, `git commit -q -m base`, monkeypatches `tr._SERVER_REPO_ROOT` to
that root, deletes `AUTONOMOUS_OVERLAY_FROM_WORKING_TREE` from the
environment, and returns `(root, spec_dir)` with `spec_dir = tmp_path / "spec"`
created. `head(root)` returns `git -C root rev-parse --short HEAD` stripped.

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
- **T10** — `_run_local_tests` puts the header on the output: with the `repo` fixture, `monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "off")`, `spec_dir / "src" / "pkg"` created holding `mod.py` = `"B\n"`, `spec_dir / "tests" / "test_new.py"` = `"def test_new():\n    assert True\n"`, and `tr._run_confined` replaced (via `mock.patch.object`) by a function `fake(raw_cmd, spec_dir, timeout, *, what, share_net=False, extra_binds=None, extra_env=None)` returning `(True, "1 passed in 0.01s\n")`: `passed, output = tr._run_local_tests(spec_dir, "pytest", 60, repo=root.name)` gives `passed is True`, `output.splitlines()[0] == f"[repo overlay] source=committed head={head(root)} dirty=0"`, and `tr.parse_overlay_ref(output).payload()["overlay_head"] == head(root)`.
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
```
