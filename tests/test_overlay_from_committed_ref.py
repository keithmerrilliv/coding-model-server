"""DEV-654: the self-target sandbox overlay is the COMMITTED tree.

`_materialize_local_repo_overlay` used to `copytree` the live working tree.
This checkout is the one a human edits while a run is in flight, so an
uncommitted edit to a file the spec never touched became what the candidate
was tested against — and nothing recorded which state the tests ran against.
Now: `git archive HEAD src`, a loud warning naming any uncommitted change
under src/ the sandbox will not see, an explicit opt-in for the old behaviour,
and a loud fallback when the root is not a checkout at all.
"""
import logging
import subprocess

import pytest

from coding_model_autonomous import test_runner as tr


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway checkout: src/pkg/mod.py = 'A' committed."""
    root = tmp_path / "checkout"; (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("A\n")
    _git(root, "init", "-q"); _git(root, "add", "."); _git(root, "commit", "-q", "-m", "base")
    monkeypatch.setattr(tr, "_SERVER_REPO_ROOT", root)
    monkeypatch.delenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", raising=False)
    spec_dir = tmp_path / "spec"; spec_dir.mkdir()
    return root, spec_dir


def _overlay(root, spec_dir):
    out = tr._materialize_local_repo_overlay(spec_dir, root.name)
    assert out is not None
    return out


def test_the_sandbox_sees_the_committed_file_not_the_working_tree(repo, caplog):
    root, spec_dir = repo
    (root / "src" / "pkg" / "mod.py").write_text("B  # uncommitted\n")   # the dev box mid-edit
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        overlay = _overlay(root, spec_dir)
    assert (overlay / "pkg" / "mod.py").read_text() == "A\n"
    msgs = [r.message for r in caplog.records]
    assert any("uncommitted change" in m and "src/pkg/mod.py" in m for m in msgs), msgs


def test_an_untracked_file_under_src_is_not_in_the_overlay(repo):
    root, spec_dir = repo
    (root / "src" / "pkg" / "scratch.py").write_text("never committed\n")
    overlay = _overlay(root, spec_dir)
    assert not (overlay / "pkg" / "scratch.py").exists()


def test_a_clean_tree_is_reported_clean(repo, caplog):
    root, spec_dir = repo
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        _overlay(root, spec_dir)
    assert any("working tree clean" in r.message for r in caplog.records)
    assert not any("uncommitted" in r.message for r in caplog.records)


def test_the_working_tree_is_an_explicit_opt_in(repo, monkeypatch, caplog):
    """Also the negative control: without the fix, the first test's assertion
    would see exactly this."""
    root, spec_dir = repo
    (root / "src" / "pkg" / "mod.py").write_text("B\n")
    monkeypatch.setenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", "1")
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        overlay = _overlay(root, spec_dir)
    assert (overlay / "pkg" / "mod.py").read_text() == "B\n"
    assert any("WORKING TREE by explicit opt-in" in r.message for r in caplog.records)


def test_a_directory_that_is_not_a_checkout_falls_back_loudly(tmp_path, monkeypatch, caplog):
    root = tmp_path / "plain"; (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("W\n")
    monkeypatch.setattr(tr, "_SERVER_REPO_ROOT", root)
    monkeypatch.delenv("AUTONOMOUS_OVERLAY_FROM_WORKING_TREE", raising=False)
    spec_dir = tmp_path / "spec"; spec_dir.mkdir()
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        overlay = _overlay(root, spec_dir)
    assert (overlay / "pkg" / "mod.py").read_text() == "W\n"
    assert any("falling back to the working tree" in r.message for r in caplog.records)


def test_the_workspace_still_shadows_the_committed_file(repo):
    """DEV-626's ordering is unchanged: the candidate's edit wins over HEAD."""
    root, spec_dir = repo
    (spec_dir / "src" / "pkg").mkdir(parents=True)
    (spec_dir / "src" / "pkg" / "mod.py").write_text("CANDIDATE\n")
    overlay = _overlay(root, spec_dir)
    assert (overlay / "pkg" / "mod.py").read_text() == "CANDIDATE\n"
