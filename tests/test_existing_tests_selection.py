"""DEV-675: the self-target pre-gate check runs the repository's own tests.

Run 32 (spec_e6dcee50) reverted DEV-672's ``repo_relative()`` and reached the
code-review gate under "compiled and the suite passed" — eight new tests, and
nothing else, had run. The existing tests that import an edited module now
run in the same pytest invocation, the output says which set each result
belongs to, and a red existing test is a TESTS_FAILED verdict before any
human gate.
"""
# DEV-689: this file spawns its own bwrap sandbox / git checkout / npm
# install, so it cannot run INSIDE the pre-gate sandbox. The
# existing-tests selection skips any file declaring this.
PREGATE_SANDBOX_UNSAFE = True

import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import coding_model_autonomous.test_runner as tr

SELF = tr._SERVER_REPO_ROOT.name


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


# ── edited modules ───────────────────────────────────────────────────────────

def test_edited_modules_are_dotted_from_the_workspace_src(tmp_path):
    ws = tmp_path / "src" / "pkg"
    ws.mkdir(parents=True)
    (ws / "mod.py").write_text("X = 1\n")
    (ws / "sub").mkdir()
    (ws / "sub" / "__init__.py").write_text("")
    (ws / "sub" / "deep.py").write_text("")
    assert tr.edited_modules(tmp_path) == ["pkg.mod", "pkg.sub", "pkg.sub.deep"]


def test_edited_modules_include_a_module_named_like_a_test(tmp_path):
    """DEV-688: everything under src/ is a module. `test_runner.py` is a
    source file in this repository, and treating it as a test file left a
    spec that edits it with NO existing tests selected — DEV-675's guard
    silently off. Only a tests/ subtree inside the package is test code."""
    ws = tmp_path / "src" / "pkg"
    ws.mkdir(parents=True)
    (ws / "test_runner.py").write_text("")
    (ws / "mod_test.py").write_text("")
    (ws / "tests").mkdir()
    (ws / "tests" / "helper.py").write_text("")
    assert tr.edited_modules(tmp_path) == ["pkg.mod_test", "pkg.test_runner"]
    assert tr.edited_modules(tmp_path / "nowhere") == []


# ── the import matcher ───────────────────────────────────────────────────────

M = "coding_model_autonomous.workspace"


@pytest.mark.parametrize("text,module,want", [
    ("from coding_model_autonomous.workspace import x\n", M, True),
    ("from coding_model_autonomous.workspace.sub import x\n", M, True),
    ("import coding_model_autonomous.workspace\n", M, True),
    ("import coding_model_autonomous.workspace as w\n", M, True),
    ("from coding_model_autonomous import workspace\n", M, True),
    ("from coding_model_autonomous import workspace as w\n", M, True),
    ("from coding_model_autonomous import (\n    Database,\n    workspace,\n)\n", M, True),
    ("    from coding_model_autonomous import workspace\n", M, True),
    ("from coding_model_autonomous.workspace_ledger import x\n", M, False),
    ("from coding_model_autonomous import Database\n", M, False),
    ("x = 'from coding_model_autonomous.workspace import y'\n", M, False),
    ("from coding_model_autonomous import Database\n", "coding_model_autonomous", True),
    ("import coding_model_autonomous\n", "coding_model_autonomous", True),
    ("import coding_model_server.orchestrator_daemon as d\n",
     "coding_model_server.orchestrator_daemon", True),
])
def test_import_matcher(text, module, want):
    assert tr.test_imports_module(text, module) is want


# ── selection over a tests tree ──────────────────────────────────────────────

@pytest.fixture
def tests_root(tmp_path):
    root = tmp_path / "repo" / "tests"
    root.mkdir(parents=True)
    (root / "conftest.py").write_text("import pkg.mod\n")
    (root / "test_mod.py").write_text("from pkg.mod import VALUE\n")
    (root / "test_pkg.py").write_text("from pkg import mod\n")
    (root / "test_other.py").write_text("from pkg.other import OTHER\n")
    (root / "helper.py").write_text("from pkg.mod import VALUE\n")
    (root / "nested").mkdir()
    (root / "nested" / "test_deep.py").write_text("import pkg.mod as m\n")
    return root


def test_selects_only_test_files_that_import_an_edited_module(tests_root):
    assert tr.existing_tests_importing(["pkg.mod"], tests_root) == [
        "tests/nested/test_deep.py", "tests/test_mod.py", "tests/test_pkg.py"]


def test_negative_control_nothing_imports_the_edited_module(tests_root):
    assert tr.existing_tests_importing(["pkg.unrelated"], tests_root) == []
    assert tr.existing_tests_importing([], tests_root) == []
    assert tr.existing_tests_importing(["pkg.mod"], tests_root / "missing") == []


def test_excluded_paths_are_the_spec_own_tests(tests_root):
    got = tr.existing_tests_importing(["pkg.mod"], tests_root,
                                      exclude=["tests/test_mod.py"])
    assert got == ["tests/nested/test_deep.py", "tests/test_pkg.py"]


# ── the mode knob and the committed tests tree ───────────────────────────────

@pytest.fixture
def self_repo(tmp_path, monkeypatch):
    """A throwaway self-target repo: committed src/ + tests/, then an
    UNCOMMITTED test edit the overlay must not see (DEV-654's rule)."""
    root = tmp_path / "coding-model-server"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (root / "src" / "pkg" / "other.py").write_text("OTHER = 2\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text("from pkg.mod import VALUE\n")
    (root / "tests" / "test_other.py").write_text("from pkg.other import OTHER\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one")
    (root / "tests" / "test_other.py").write_text("from pkg.mod import VALUE  # uncommitted\n")
    monkeypatch.setattr(tr, "_SERVER_REPO_ROOT", root)
    monkeypatch.delenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", raising=False)
    return root


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec_x"
    (d / "src" / "pkg").mkdir(parents=True)
    (d / "src" / "pkg" / "mod.py").write_text("VALUE = 2\n")
    (d / "tests").mkdir()
    (d / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    return d


def test_imports_mode_selects_from_the_committed_tests_tree(self_repo, spec_dir, monkeypatch):
    monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    mode, selected = tr.select_existing_tests(spec_dir, overlay_root)
    assert mode == "imports"
    # test_other.py imports pkg.mod only in the WORKING tree — not selected.
    assert selected == ["tests/test_mod.py"]
    assert (overlay_root / "tests" / "test_other.py").read_text() == "from pkg.other import OTHER\n"


def test_all_mode_selects_the_whole_tree(self_repo, spec_dir, monkeypatch):
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "all")
    mode, selected = tr.select_existing_tests(spec_dir, spec_dir / tr._REPO_OVERLAY_DIR)
    assert (mode, selected) == ("all", ["tests/test_mod.py", "tests/test_other.py"])


def test_off_mode_selects_nothing_and_reads_no_tree(self_repo, spec_dir, monkeypatch):
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "off")
    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    assert tr.select_existing_tests(spec_dir, overlay_root) == ("off", [])
    assert not (overlay_root / "tests").exists()


def test_a_test_file_the_spec_modified_runs_only_from_the_workspace(self_repo, spec_dir, monkeypatch):
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "all")
    (spec_dir / "tests" / "test_mod.py").write_text("from pkg.mod import VALUE\n")
    _, selected = tr.select_existing_tests(spec_dir, spec_dir / tr._REPO_OVERLAY_DIR)
    assert selected == ["tests/test_other.py"]


def test_an_unknown_mode_falls_back_to_imports(monkeypatch, caplog):
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "sometimes")
    assert tr.existing_tests_mode() == "imports"
    assert any("DEV-675" in r.getMessage() for r in caplog.records)


def test_an_unreadable_tree_selects_nothing_loudly(self_repo, spec_dir, monkeypatch, caplog):
    monkeypatch.setattr(tr, "_extract_committed_tree",
                        mock.Mock(side_effect=RuntimeError("no git")))
    mode, selected = tr.select_existing_tests(spec_dir, spec_dir / tr._REPO_OVERLAY_DIR)
    assert (mode, selected) == ("imports", [])
    assert any("existing tests will NOT run" in r.getMessage() for r in caplog.records)


def test_the_whole_committed_tree_is_materialized_beside_the_src_overlay(self_repo, spec_dir, monkeypatch):
    """Existing tests read the repository by path (docs/PIPELINE.md,
    .env.example): the overlay carries the committed tree, not tests/ alone,
    and leaves the DEV-626 src/ overlay untouched."""
    monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
    (self_repo / "docs").mkdir()
    (self_repo / "docs" / "GUIDE.md").write_text("committed guide\n")
    (self_repo / "README.md").write_text("readme\n")
    _git(self_repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "docs", "README.md")
    _git(self_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "docs")
    (self_repo / "docs" / "GUIDE.md").write_text("uncommitted guide\n")
    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    (overlay_root / "src" / "pkg").mkdir(parents=True)
    (overlay_root / "src" / "pkg" / "mod.py").write_text("OVERLAY = 1\n")
    (overlay_root / "stale.txt").write_text("from a previous attempt\n")

    mode, selected = tr.select_existing_tests(spec_dir, overlay_root)

    assert selected == ["tests/test_mod.py"]
    assert (overlay_root / "docs" / "GUIDE.md").read_text() == "committed guide\n"
    assert (overlay_root / "README.md").read_text() == "readme\n"
    assert (overlay_root / "src" / "pkg" / "mod.py").read_text() == "OVERLAY = 1\n"
    assert not (overlay_root / "src" / "pkg" / "other.py").exists()
    assert not (overlay_root / "stale.txt").exists()


def test_workspace_edits_outside_src_shadow_the_committed_copy(self_repo, spec_dir, monkeypatch):
    """A candidate that edits docs/GUIDE.md alongside a module is judged
    against ITS docs — the src/ rule, applied to the rest of the tree. The
    workspace's own artifacts are not repository files and stay out."""
    monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
    (self_repo / "docs").mkdir()
    (self_repo / "docs" / "GUIDE.md").write_text("committed guide\n")
    _git(self_repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "docs")
    _git(self_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "docs")
    (spec_dir / "docs").mkdir()
    (spec_dir / "docs" / "GUIDE.md").write_text("candidate guide\n")
    (spec_dir / "design.md").write_text("# design\n")
    (spec_dir / "tests" / "test_mod.py").write_text("from pkg.mod import VALUE  # edited\n")
    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR

    _, selected = tr.select_existing_tests(spec_dir, overlay_root)

    assert (overlay_root / "docs" / "GUIDE.md").read_text() == "candidate guide\n"
    assert not (overlay_root / "design.md").exists()
    # The edited test file runs from the workspace, not the overlay.
    assert selected == []


def test_the_workspace_src_is_never_a_collection_target(spec_dir):
    """DEV-688: src/ holds the attempt's source, which the overlay puts on
    PYTHONPATH. Collecting it imports a source file named test_*.py as a test
    module under a synthesized `src.<pkg>` package, and its relative imports
    then fail — a collection error no model can fix."""
    (spec_dir / "src" / "pkg" / "test_runner.py").write_text("from . import sibling\n")
    targets = tr._collection_targets(spec_dir)
    assert str(spec_dir / "src") not in targets
    assert str(spec_dir / "tests") in targets


def test_a_source_file_named_like_a_test_is_not_collected(self_repo, spec_dir, monkeypatch):
    """The same guard on the command line: the run ignores the workspace's
    src/ whichever target shape it uses."""
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "off")
    (spec_dir / "src" / "pkg" / "test_runner.py").write_text("from . import sibling\n")
    captured = {}
    with mock.patch.object(tr, "_run_confined", _capture_confined(captured)):
        tr._run_local_tests(spec_dir, "pytest", 60, repo=self_repo.name)
    cmd = captured["raw_cmd"]
    ignored = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--ignore"}
    assert str(spec_dir / "src") in ignored


def test_a_file_that_cannot_run_nested_is_never_selected(self_repo, spec_dir, monkeypatch):
    """DEV-689: a repository test that spawns its own sandbox, git checkout or
    npm install cannot run inside the pre-gate sandbox. It declares
    PREGATE_SANDBOX_UNSAFE and the selection skips it, so it never reds an
    attempt for something the model did not do."""
    monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
    (self_repo / "tests" / "test_nested.py").write_text(
        "PREGATE_SANDBOX_UNSAFE = True\nfrom pkg.mod import VALUE\n")
    _git(self_repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "tests/test_nested.py")
    _git(self_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "nested")

    _, selected = tr.select_existing_tests(spec_dir, spec_dir / tr._REPO_OVERLAY_DIR)
    assert selected == ["tests/test_mod.py"]          # imports mode

    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "all")
    _, every = tr.select_existing_tests(spec_dir, spec_dir / tr._REPO_OVERLAY_DIR)
    assert "tests/test_nested.py" not in every        # and in all mode


# ── the command line and the output header ───────────────────────────────────

def _capture_confined(captured):
    def fake(raw_cmd, spec_dir, timeout, *, what, share_net=False,
             extra_binds=None, extra_env=None):
        captured["raw_cmd"] = raw_cmd
        captured["extra_env"] = extra_env
        return True, "tests/test_new.py::test_new PASSED\n1 passed in 0.01s\n"
    return fake


def test_selected_existing_tests_are_on_the_pytest_command_line(self_repo, spec_dir, monkeypatch):
    monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
    monkeypatch.setattr(tr, "_SERVER_REPO_ROOT", self_repo)
    captured = {}
    with mock.patch.object(tr, "_run_confined", _capture_confined(captured)):
        passed, output = tr._run_local_tests(spec_dir, "pytest", 60, repo=self_repo.name)
    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    cmd = captured["raw_cmd"]
    assert cmd[-1] == str(overlay_root / "tests" / "test_mod.py")
    assert str(overlay_root / "tests" / "test_other.py") not in cmd
    # spec_dir's entries stand in for spec_dir (a directory argument would
    # make pytest drop the explicit file under the dot-directory overlay).
    assert cmd.count(str(spec_dir)) == 1 and cmd[cmd.index(str(spec_dir)) - 1] == "--rootdir"
    # DEV-688: the workspace's src/ is NOT a collection target.
    assert cmd[-2] == str(spec_dir / "tests")
    assert str(spec_dir / "src") not in cmd[cmd.index("--rootdir"):]
    assert cmd[cmd.index("--rootdir") + 1] == str(spec_dir)
    assert str(overlay_root) in cmd[cmd.index("--ignore"):]
    # The overlay src first, then the selected file's own directory — what
    # prepend mode would have inserted for a conftest's sibling imports.
    assert captured["extra_env"] == {"PYTHONPATH": os.pathsep.join(
        [str(overlay_root / "src"), str(overlay_root / "tests")])}
    assert passed is True
    header, _, rest = output.partition("\n")
    assert header == (f"{tr.EXISTING_TESTS_MARKER} mode=imports selected=1 "
                      "for edited pkg.mod: tests/test_mod.py")
    assert rest.startswith("tests/test_new.py::test_new PASSED")


def test_no_selection_keeps_the_directory_argument(self_repo, spec_dir, monkeypatch):
    """A spec that edits nothing anything imports runs exactly as before."""
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "off")
    captured = {}
    with mock.patch.object(tr, "_run_confined", _capture_confined(captured)):
        _, output = tr._run_local_tests(spec_dir, "pytest", 60, repo=self_repo.name)
    cmd = captured["raw_cmd"]
    assert cmd[-1] == str(spec_dir) and "--rootdir" not in cmd
    assert captured["extra_env"] == {"PYTHONPATH": str(spec_dir / tr._REPO_OVERLAY_DIR / "src")}
    assert output.startswith(f"{tr.EXISTING_TESTS_MARKER} mode=off selected=0")


def test_a_foreign_repo_gets_no_header(spec_dir):
    captured = {}
    with mock.patch.object(tr, "_run_confined", _capture_confined(captured)):
        _, output = tr._run_local_tests(spec_dir, "pytest", 60, repo="centipede")
    assert tr.EXISTING_TESTS_MARKER not in output
    assert tr.parse_test_split(output) is None


def test_header_stays_short_for_all_mode():
    selected = [f"tests/test_{i}.py" for i in range(200)]
    header = tr.existing_tests_header("all", selected)
    assert len(header) < 400 and "(+188 more)" in header and "selected=200" in header


# ── the split parser ─────────────────────────────────────────────────────────

MIXED = """[self-target existing tests] mode=imports selected=2 for edited pkg.mod: tests/test_a.py, tests/test_b.py
============================= test session starts ==============================
tests/test_new.py::test_1 PASSED                                          [ 16%]
tests/test_new.py::test_2 FAILED                                          [ 33%]
tests/test_new.py::test_3 SKIPPED (why)                                   [ 50%]
.repo_overlay/tests/test_a.py::test_x PASSED                              [ 66%]
.repo_overlay/tests/test_a.py::test_y FAILED                              [ 83%]
.repo_overlay/tests/test_b.py::TestK::test_z ERROR                        [100%]
=========================== short test summary info ============================
FAILED tests/test_new.py::test_2 - AssertionError
FAILED .repo_overlay/tests/test_a.py::test_y - AssertionError: assert 1 == 2
ERROR .repo_overlay/tests/test_b.py::TestK::test_z - fixture 'nope' not found
==================== 2 failed, 2 passed, 1 skipped, 1 error in 0.10s ==========
"""


def test_split_counts_new_and_existing_separately():
    split = tr.parse_test_split(MIXED)
    assert split is not None
    assert (split.mode, split.selected) == ("imports", 2)
    assert (split.new_passed, split.new_failed) == (1, 1)
    assert (split.existing_passed, split.existing_failed) == (1, 2)
    assert split.existing_failed_ids == [".repo_overlay/tests/test_a.py::test_y",
                                         ".repo_overlay/tests/test_b.py::TestK::test_z"]
    assert split.new_failed_ids == ["tests/test_new.py::test_2"]
    assert split.payload()["existing_tests"] == {"passed": 1, "failed": 2}
    assert split.payload()["new_tests"] == {"passed": 1, "failed": 1}


def test_split_with_rootdir_relative_ids_still_finds_the_overlay():
    out = ("[self-target existing tests] mode=all selected=1: tests/test_a.py\n"
           "var/tasks_db/specs/spec_1/tests/test_new.py::t PASSED\n"
           "var/tasks_db/specs/spec_1/.repo_overlay/tests/test_a.py::t PASSED\n"
           "2 passed in 0.01s\n")
    split = tr.parse_test_split(out)
    assert (split.new_passed, split.existing_passed) == (1, 1)


def test_no_header_means_no_split():
    assert tr.parse_test_split("tests/test_new.py::t PASSED\n1 passed in 0.01s\n") is None
    assert tr.parse_test_split("") is None


# ── the real sandbox, with a negative control ────────────────────────────────

REAL_MODULE = "src/coding_model_autonomous/workspace.py"
REAL_TEST = "tests/test_placeholder_vocabulary.py"


def _head_file(rel: str) -> str:
    return subprocess.run(["git", "-C", str(tr._SERVER_REPO_ROOT), "show", f"HEAD:{rel}"],
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(not tr._sandbox_available(), reason="bwrap sandbox unavailable")
@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
class TestInTheRealSandbox:
    """Run 32's shape, end to end: a workspace edit to a real module of this
    repository, and the existing test that pins it. The control is the same
    workspace with the module unchanged."""

    def _spec(self, tmp_path, module_text: str) -> Path:
        spec_dir = tmp_path / "spec_real"
        mod = spec_dir / REAL_MODULE
        mod.parent.mkdir(parents=True)
        mod.write_text(module_text)
        (spec_dir / "tests").mkdir()
        (spec_dir / "tests" / "test_new.py").write_text(
            "def test_new():\n    assert True\n")
        return spec_dir

    def test_a_reverted_module_reds_the_existing_test(self, tmp_path, monkeypatch):
        monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
        broken = _head_file(REAL_MODULE) + (
            "\n\ndef is_placeholder_path(path):  # reverted behaviour\n"
            "    return False\n")
        spec_dir = self._spec(tmp_path, broken)
        passed, output = tr.run_tests(spec_dir, framework="pytest", timeout=240, repo=SELF)
        split = tr.parse_test_split(output)
        assert passed is False, output[-3000:]
        assert split is not None and split.mode == "imports"
        assert split.new_passed == 1 and split.new_failed == 0
        assert any(REAL_TEST in i for i in split.existing_failed_ids), output[-3000:]

    def test_control_the_unchanged_module_is_green(self, tmp_path, monkeypatch):
        monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
        spec_dir = self._spec(tmp_path, _head_file(REAL_MODULE))
        passed, output = tr.run_tests(spec_dir, framework="pytest", timeout=240, repo=SELF)
        split = tr.parse_test_split(output)
        assert passed is True, output[-3000:]
        assert split is not None and split.existing_failed_ids == []
        assert split.existing_passed > 0 and split.new_passed == 1
        assert f"selected={split.selected}" in output.splitlines()[0]
        assert REAL_TEST in output.splitlines()[0]
