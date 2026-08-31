"""DEV-621: descriptive change-surface tables must still feed the fetch.

Run 19's hand-written table used a descriptive second column, so every row
failed the keyword regex and the existing-file fetch lost the table — the
DEV-492 guard and the working-blind warning were both silently disarmed.
Backticked path rows are now a weaker declaration tier: fetch candidates,
never hard-stops.
"""
from types import SimpleNamespace

import coding_model_server.orchestrator_daemon as d

# Run 19's table, verbatim shape (descriptive second column).
RUN19_TABLE = """
## Change surface

| Path | What changes here |
|---|---|
| `src/coding_model_autonomous/executor.py` | `_write_artifact` / artifact recording |
| `src/coding_model_server/orchestrator_daemon.py` | record the tested-artifact manifest |
| `src/coding_model_autonomous/delivery.py` | hash comparison before push |
"""

KEYWORD_TABLE = """
| Path | Action |
|---|---|
| `src/a.py` | modify — add the guard |
| `src/b.py` | modified in place |
| `src/c.py` | new file |
"""

PROSE_ONLY = """
The implementation may modify executor.py and delivery.py as needed.
No table here.
"""


class TestPathRowTier:
    def test_run19_descriptive_table_yields_path_rows(self):
        assert d._change_surface_path_rows(RUN19_TABLE) == [
            "src/coding_model_autonomous/executor.py",
            "src/coding_model_server/orchestrator_daemon.py",
            "src/coding_model_autonomous/delivery.py",
        ]

    def test_run19_table_still_has_no_keyword_declarations(self):
        # The hard-stop tier stays keyword-only (greenfield tables must plan).
        assert d._declared_file_modifications(RUN19_TABLE) == []

    def test_keyword_table_declares_only_modify_rows(self):
        assert d._declared_file_modifications(KEYWORD_TABLE) == [
            "src/a.py", "src/b.py"]

    def test_keyword_rows_also_appear_as_path_rows(self):
        assert set(d._change_surface_path_rows(KEYWORD_TABLE)) == {
            "src/a.py", "src/b.py", "src/c.py"}

    def test_prose_without_backticked_table_yields_nothing(self):
        assert d._change_surface_path_rows(PROSE_ONLY) == []
        assert d._declared_file_modifications(PROSE_ONLY) == []

    def test_header_and_non_path_cells_excluded(self):
        md = "| `Path` | x |\n|---|---|\n| `notes` | y |\n| `src/f.py` | z |\n"
        assert d._change_surface_path_rows(md) == ["src/f.py"]

    def test_empty_spec(self):
        assert d._change_surface_path_rows("") == []


class TestFetchCandidates:
    def _spec(self):
        return SimpleNamespace(id="spec_test")

    def test_descriptive_table_paths_reach_the_fetch(self, monkeypatch):
        requested: list = []

        def fake_fetch(repo, paths, base_ref="HEAD", timeout=30):
            requested.extend(paths)
            return [(p, "content") for p in paths], []

        monkeypatch.setattr(d, "_load_plan",
                            lambda spec: {"test_strategy": {"repo": "r"}})
        monkeypatch.setattr(d.test_runner, "fetch_repo_files", fake_fetch)
        files = d._fetch_existing_files_for_spec(self._spec(), RUN19_TABLE)
        assert "src/coding_model_server/orchestrator_daemon.py" in requested
        assert len(files) == 3

    def test_extra_paths_still_merge_and_dedup(self, monkeypatch):
        requested: list = []

        def fake_fetch(repo, paths, base_ref="HEAD", timeout=30):
            requested.extend(paths)
            return [], []

        monkeypatch.setattr(d, "_load_plan",
                            lambda spec: {"test_strategy": {"repo": "r"}})
        monkeypatch.setattr(d.test_runner, "fetch_repo_files", fake_fetch)
        d._fetch_existing_files_for_spec(
            self._spec(), RUN19_TABLE,
            extra_paths=["src/coding_model_autonomous/delivery.py", "new.py"])
        assert requested.count("src/coding_model_autonomous/delivery.py") == 1
        assert "new.py" in requested
