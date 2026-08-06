"""The mac-runner can serve file contents at a ref (DEV-492).

The implementer is never given the contents of files it must modify, so a spec
saying "change line 163" produces a whole-file reconstruction invented from the
spec alone. This endpoint is the missing read path: the repos live on the Mac,
so the Mac is the only host that can answer.

Reads go through `git show <ref>:<path>` — no worktree, no checkout. These
tests drive real git (the repo fixture is a real repository) and deliberately
do NOT stub subprocess, because resolving a path at a non-HEAD ref is the
behaviour under test.
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from mac_runner import server
from mac_runner.config import Config


def _commit(path, message):
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Two commits, so `base_ref` can be shown to matter."""
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "Strategy.swift").write_text("// original\nstruct A {}\n")
    first = _commit(path, "init")
    (path / "Strategy.swift").write_text("// rewritten\nstruct A {}\nstruct B {}\n")
    _commit(path, "second")
    return path, first


@pytest.fixture
def client(tmp_path, repo, monkeypatch):
    path, _ = repo
    repos_file = tmp_path / "repos.yml"
    repos_file.write_text(f"repos:\n  proj:\n    path: {path}\n")
    monkeypatch.setattr(Config, "REPOS_FILE", repos_file)
    monkeypatch.setattr(Config, "API_KEY", "test-key")
    return TestClient(server.app)


HEADERS = {"X-Runner-Key": "test-key"}


def _read(client, **body):
    body.setdefault("repo", "proj")
    return client.post("/v1/read_files", headers=HEADERS, json=body)


def test_returns_file_contents_at_head(client):
    resp = _read(client, paths=["Strategy.swift"])
    assert resp.status_code == 200
    (file,) = resp.json()["files"]
    assert file["content"] == "// rewritten\nstruct A {}\nstruct B {}\n"
    assert file["error"] is None


def test_reads_the_requested_ref_not_the_working_tree(client, repo):
    """The whole point: the implementer must see the file as it is at base_ref.

    Reading HEAD instead would hand it content that does not match the commit
    the runner will build from.
    """
    _, first = repo
    (file,) = _read(client, base_ref=first, paths=["Strategy.swift"]).json()["files"]
    assert file["content"] == "// original\nstruct A {}\n"


def test_one_bad_path_does_not_deny_the_others(client):
    """Partial answers beat none — a design naming one stale path must not cost
    the implementer the eight files that do exist."""
    files = _read(client, paths=["Strategy.swift", "Gone.swift"]).json()["files"]
    by_path = {f["path"]: f for f in files}
    assert by_path["Strategy.swift"]["content"].startswith("// rewritten")
    assert by_path["Gone.swift"]["content"] is None
    assert "does not exist" in by_path["Gone.swift"]["error"]


@pytest.mark.parametrize("bad,reason", [
    ("/etc/passwd", "absolute"),
    ("../outside.txt", "escapes"),
    ("", "empty"),
])
def test_unsafe_paths_are_refused_in_band(client, bad, reason):
    (file,) = _read(client, paths=[bad]).json()["files"]
    assert file["content"] is None
    assert reason in file["error"]


def test_unknown_repo_is_rejected(client):
    """Same allowlist as run_tests: the runner refuses paths not in repos.yml."""
    assert _read(client, repo="not-registered", paths=["x"]).status_code == 400


def test_requires_the_runner_key(client):
    resp = client.post("/v1/read_files", json={"repo": "proj", "paths": ["x"]})
    assert resp.status_code == 401


def test_oversized_file_reports_instead_of_flooding_the_prompt(client, repo,
                                                               monkeypatch):
    """Caps protect the implementer's token budget, not the runner's memory."""
    path, _ = repo
    (path / "Big.swift").write_text("x" * 5000)
    _commit(path, "big")
    monkeypatch.setattr(server, "READ_FILES_PER_FILE_MAX_BYTES", 1000)
    (file,) = _read(client, paths=["Big.swift"]).json()["files"]
    assert file["content"] is None
    assert "per-file cap" in file["error"]


def test_binary_file_reports_instead_of_raising(client, repo):
    path, _ = repo
    (path / "logo.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    _commit(path, "binary")
    (file,) = _read(client, paths=["logo.bin"]).json()["files"]
    assert file["content"] is None
    assert "UTF-8" in file["error"]


def test_path_count_is_capped(client, monkeypatch):
    monkeypatch.setattr(server, "READ_FILES_MAX_PATHS", 2)
    files = _read(client, paths=["Strategy.swift"] * 5).json()["files"]
    assert len(files) == 2
