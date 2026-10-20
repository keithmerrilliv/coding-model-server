"""DEV-681: OverlayRef tracking for self-target sandbox (TEST_RAN payload).

Tests all 11 acceptance criteria for the OverlayRef feature that records
which repository state a self-target sandbox's overlay was built from.

Import rules: use `from coding_model_autonomous import test_runner as tr`.
Never `from src...` - the repo's src/ directory is the package root on sys.path,
so it's NOT itself a package. Import via the package name only.
"""
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coding_model_autonomous import test_runner as tr


def _git(root, *args):
    """Run git with check=True and capture_output=True."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True)


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway self-target repo: committed src/, then an UNCOMMITTED edit.

    The fixture:
      - creates tmp_path / "checkout" with src/pkg/mod.py = "A\n"
      - runs git init/add/commit
      - monkeypatches tr._SERVER_REPO_ROOT to that checkout
      - deletes AUTONOMOUS_OVERLAY_FROM_WORKING_TREE env var
      - returns (root, spec_dir) where spec_dir = tmp_path / "spec"
    """
    root = tmp_path / "checkout"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("A\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    # Monkeypatch BEFORE returning so tests see the patched value
    monkeypatch.setattr(tr, "_SERVER_REPO_ROOT", root)
    monkeypatch.delenv(tr.EXISTING_TESTS_MODE_ENV, raising=False)
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    return root, spec_dir


def head(root: Path) -> str:
    """Return the short SHA of root's HEAD."""
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()


# ── T1 — OverlayRef.header() and payload() ───────────────────────────────────

def test_overlay_ref_header_and_payload():
    ref = tr.OverlayRef("committed", "abc1234", 0)
    assert ref.header() == "[repo overlay] source=committed head=abc1234 dirty=0"
    assert ref.payload() == {
        "overlay_source": "committed",
        "overlay_head": "abc1234",
        "overlay_dirty": 0
    }


# ── T2 — parse_overlay_ref with existing-tests header ───────────────────────

def test_parse_overlay_ref_with_existing_tests_header():
    output = (
        "[self-target existing tests] mode=imports selected=1: tests/test_x.py\n"
        "[repo overlay] source=committed head=abc1234 dirty=2\n"
        "1 passed\n"
    )
    got = tr.parse_overlay_ref(output)
    assert got == tr.OverlayRef("committed", "abc1234", 2)


# ── T3 — parse_overlay_ref without overlay header ────────────────────────────

def test_parse_overlay_ref_without_header():
    r1 = tr.parse_overlay_ref("1 passed in 0.01s\n")
    r2 = tr.parse_overlay_ref("")
    assert r1 is None
    assert r2 is None


# ── T4 — note_overlay_ref and read_overlay_ref round-trip ───────────────────

def test_note_and_read_overlay_ref(tmp_path):
    ov = tmp_path / "ov"
    tr.note_overlay_ref(ov, "committed", "abc1234", ["src/pkg/mod.py"])

    # Check file exists and has correct content
    ref_file = ov / "OVERLAY_REF.json"
    assert ref_file.is_file()
    data = json.loads(ref_file.read_text())
    assert data == {
        "source": "committed",
        "head": "abc1234",
        "dirty": ["src/pkg/mod.py"]
    }

    # Read back and verify
    got = tr.read_overlay_ref(ov)
    assert got == tr.OverlayRef("committed", "abc1234", 1)  # dirty=1 from len(dirty)


# ── T5 — read_overlay_ref on missing/unparseable files ──────────────────────

def test_read_overlay_ref_missing_or_bad(tmp_path):
    assert tr.read_overlay_ref(tmp_path / "nowhere") is None

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "OVERLAY_REF.json").write_text("not json")
    assert tr.read_overlay_ref(bad) is None


# ── T6 — _materialize_local_repo_overlay produces committed ref (clean tree) ─

def test_materialize_committed_clean_tree(repo):
    root, spec_dir = repo
    sha = head(root)

    result = tr._materialize_local_repo_overlay(spec_dir, root.name)
    assert result is not None

    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    ref = tr.read_overlay_ref(overlay_root)
    assert ref.source == "committed"
    assert ref.head == sha
    assert ref.dirty == 0


# ── T7 — After uncommitted edit, still committed branch but dirty=1 ──────────

def test_materialize_with_uncommitted_edit(repo):
    root, spec_dir = repo
    (root / "src" / "pkg" / "mod.py").write_text("B\n")
    sha = head(root)

    result = tr._materialize_local_repo_overlay(spec_dir, root.name)
    assert result is not None

    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    ref = tr.read_overlay_ref(overlay_root)
    assert ref == tr.OverlayRef("committed", sha, 1)

    # Verify JSON has the actual path list
    data = json.loads((overlay_root / "OVERLAY_REF.json").read_text())
    assert data["dirty"] == ["src/pkg/mod.py"]


# ── T8 — AUTONOMOUS_OVERLAY_FROM_WORKING_TREE=1 gives working_tree source ────

def test_working_tree_branch(repo, monkeypatch):
    root, spec_dir = repo
    monkeypatch.setenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", "1")

    result = tr._materialize_local_repo_overlay(spec_dir, root.name)
    assert result is not None

    overlay_root = spec_dir / tr._REPO_OVERLAY_DIR
    ref = tr.read_overlay_ref(overlay_root)
    assert ref.source == "working_tree"
    assert ref.head == head(root)  # real HEAD sha


# ── T9 — Non-git directory triggers fallback branch ──────────────────────────

def test_fallback_non_git_directory(tmp_path, monkeypatch):
    ng = tmp_path / "ng"
    ng.mkdir()
    (ng / "src" / "pkg").mkdir(parents=True)
    (ng / "src" / "pkg" / "mod.py").touch()

    with mock.patch.object(tr, "_SERVER_REPO_ROOT", ng):
        result = tr._materialize_local_repo_overlay(tmp_path / "spec", ng.name)
    assert result is not None

    overlay_root = tmp_path / "spec" / tr._REPO_OVERLAY_DIR
    ref = tr.read_overlay_ref(overlay_root)
    assert ref == tr.OverlayRef("fallback", "unknown", 0)


# ── T10 — _run_local_tests prepends two-header block ────────────────────────

def test_run_local_tests_two_headers(repo, monkeypatch):
    root, spec_dir = repo
    monkeypatch.setenv(tr.EXISTING_TESTS_MODE_ENV, "off")

    # Create the required structure
    (spec_dir / "src" / "pkg").mkdir(parents=True)
    (spec_dir / "src" / "pkg" / "mod.py").write_text("B\n")
    (spec_dir / "tests").mkdir()
    (spec_dir / "tests" / "test_new.py").write_text(
        "def test_new():\n    assert True\n"
    )

    def fake(raw_cmd, spec_dir_arg, timeout, *, what, share_net=False,
             extra_binds=None, extra_env=None):
        return (True, "1 passed in 0.01s\n")

    with mock.patch.object(tr, "_run_confined", fake):
        passed, output = tr._run_local_tests(spec_dir, "pytest", 60, repo=root.name)

    lines = output.splitlines()
    assert passed is True
    assert lines[0].startswith("[self-target existing tests] mode=off")
    expected_overlay = f"[repo overlay] source=committed head={head(root)} dirty=0"
    assert lines[1] == expected_overlay

    parsed = tr.parse_overlay_ref(output)
    assert parsed is not None
    assert parsed.payload()["overlay_head"] == head(root)


# ── T11 — Same as T10 but mode=imports after committing a test ───────────────

def test_run_local_tests_imports_mode_with_committed_test(repo, monkeypatch):
    root, spec_dir = repo

    # Commit a test file to the checkout
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text("from pkg.mod import X\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "tests/test_mod.py")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "t")

    def fake(raw_cmd, spec_dir_arg, timeout, *, what, share_net=False,
             extra_binds=None, extra_env=None):
        return (True, "1 passed in 0.01s\n")

    with mock.patch.object(tr, "_run_confined", fake):
        passed, output = tr._run_local_tests(spec_dir, "pytest", 60, repo=root.name)

    lines = output.splitlines()
    assert passed is True
    assert lines[0].startswith("[self-target existing tests] mode=imports")
    assert lines[1].startswith("[repo overlay] source=committed")


# ── Additional edge cases ────────────────────────────────────────────────────

def test_overlay_ref_with_dirty_paths():
    ref = tr.OverlayRef("working_tree", "abc1234", 3)
    assert ref.header() == "[repo overlay] source=working_tree head=abc1234 dirty=3"
    payload = ref.payload()
    assert payload["overlay_source"] == "working_tree"
    assert payload["overlay_head"] == "abc1234"
    assert payload["overlay_dirty"] == 3


def test_parse_overlay_ref_only_header_line():
    header = "[repo overlay] source=fallback head=unknown dirty=0"
    got = tr.parse_overlay_ref(header)
    assert got == tr.OverlayRef("fallback", "unknown", 0)


def test_note_creates_directory_if_missing(tmp_path):
    ov = tmp_path / "new" / "nested" / "dir"
    tr.note_overlay_ref(ov, "committed", "sha123", [])

    ref_file = ov / "OVERLAY_REF.json"
    assert ref_file.is_file()
    data = json.loads(ref_file.read_text())
    assert data["source"] == "committed"


def test_read_returns_none_on_invalid_json(tmp_path):
    bad_dir = tmp_path / "badjson"
    bad_dir.mkdir()
    (bad_dir / "OVERLAY_REF.json").write_text("{invalid")
    assert tr.read_overlay_ref(bad_dir) is None


def test_read_returns_none_on_missing_key(tmp_path):
    bad_dir = tmp_path / "missingkey"
    bad_dir.mkdir()
    (bad_dir / "OVERLAY_REF.json").write_text('{"source": "committed"}')
    assert tr.read_overlay_ref(bad_dir) is None
