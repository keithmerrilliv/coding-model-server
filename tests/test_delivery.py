"""DEV-535 — a fully approved run must leave the change on a branch of the
target repo, or say plainly that it did not.

Four verified runs ended with Jira Done and a byte-identical target repo;
the only surviving copy of each change was scratch state under var/. These
tests drive the delivery step against a real local bare repo so the git
mechanics (clone, branch, commit, force-push) are exercised for real.
"""
import subprocess

import pytest

from coding_model_autonomous import delivery


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


@pytest.fixture
def remote(tmp_path):
    """A bare 'origin' seeded with one commit on its default branch."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), "seed")
    (seed / "README.md").write_text("hello\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    _git(seed, "push", "origin", "HEAD")
    return bare


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec_ws"
    (d / "Sources").mkdir(parents=True)
    (d / "Sources" / "Thing.swift").write_text("struct Thing {}\n")
    (d / "Sources" / "Protected.swift").write_text("MUST NOT SHIP\n")
    return d


def test_pushes_code_artifacts_to_a_pipeline_branch(remote, spec_dir, tmp_path,
                                                    monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    r = delivery.deliver_spec(
        "spec_t1", "Demo spec", spec_dir,
        ["Sources/Thing.swift", "Sources/Protected.swift"],
        repo_name="demo", protected_paths=["Sources/Protected.swift"])
    assert r.status == "pushed", r.detail
    assert r.branch == "pipeline/spec_t1"

    check = tmp_path / "check"
    _git(tmp_path, "clone", "--branch", "pipeline/spec_t1", str(remote), "check")
    assert (check / "Sources" / "Thing.swift").read_text() == "struct Thing {}\n"
    assert not (check / "Sources" / "Protected.swift").exists(), \
        "protected paths must never be delivered"
    log = _git(check, "log", "-1", "--format=%an %s")
    assert "coding-model-pipeline" in log and "spec_t1" in log


def test_redelivery_force_updates_the_pipeline_branch(remote, spec_dir, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    args = ("spec_t1", "Demo spec", spec_dir, ["Sources/Thing.swift"], "demo", [])
    assert delivery.deliver_spec(*args).status == "pushed"
    (spec_dir / "Sources" / "Thing.swift").write_text("struct Thing { let v = 2 }\n")
    r = delivery.deliver_spec(*args)
    assert r.status == "pushed", r.detail
    _git(tmp_path, "clone", "--branch", "pipeline/spec_t1", str(remote), "check2")
    assert "v = 2" in (tmp_path / "check2" / "Sources" / "Thing.swift").read_text()


def test_no_repo_name_is_an_honest_skip(spec_dir):
    r = delivery.deliver_spec("spec_t2", "Greenfield", spec_dir,
                              ["Sources/Thing.swift"], None, [])
    assert r.status == "skipped"
    assert "workspace" in r.detail


def test_unconfigured_remote_is_an_honest_skip(spec_dir, monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_REMOTES", raising=False)
    r = delivery.deliver_spec("spec_t3", "Demo", spec_dir,
                              ["Sources/Thing.swift"], "demo", [])
    assert r.status == "skipped"
    assert "AUTONOMOUS_DELIVERY_REMOTES" in r.detail


def test_content_already_on_default_branch_skips(remote, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "README.md").write_text("hello\n")  # identical to the seed commit
    r = delivery.deliver_spec("spec_t4", "Demo", ws, ["README.md"], "demo", [])
    assert r.status == "skipped"
    assert "byte-for-byte" in r.detail


def test_unreachable_remote_fails_open(spec_dir, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES",
                       "demo=/nonexistent/nowhere.git")
    r = delivery.deliver_spec("spec_t5", "Demo", spec_dir,
                              ["Sources/Thing.swift"], "demo", [])
    assert r.status == "failed"
    assert "clone" in r.detail


def test_remotes_parser_handles_multiple_pairs(monkeypatch):
    monkeypatch.setenv(
        "AUTONOMOUS_DELIVERY_REMOTES",
        "electric-sheep=git@github.com:k/ES.git, centipede=git@github.com:k/C.git")
    remotes = delivery.delivery_remotes()
    assert remotes == {"electric-sheep": "git@github.com:k/ES.git",
                       "centipede": "git@github.com:k/C.git"}
