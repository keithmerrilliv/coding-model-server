"""DEV-674 — a self-target read comes from this repository's own HEAD.

The Mac runner's clone of coding-model-server is at whatever commit it was
last pulled to; the sandbox overlay is `git archive HEAD` of the daemon's own
checkout. Run 32 fetched a pre-DEV-672 outcome.py from the Mac, edited it,
and delivered a reversion with every new test green. Self-target reads now
answer from the local repository, in the runner's exact shape.
"""
import subprocess

import pytest

from coding_model_autonomous import test_runner


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def self_repo(tmp_path, monkeypatch):
    root = tmp_path / "coding-model-server"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (root / "src" / "pkg" / "other.py").write_text("OTHER = 2\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one")
    # An uncommitted edit must NOT be what a read returns (DEV-654's rule).
    (root / "src" / "pkg" / "mod.py").write_text("VALUE = 999\n")
    monkeypatch.setattr(test_runner, "_SERVER_REPO_ROOT", root)

    def never(*a, **k):
        raise AssertionError("a self-target read must not reach the runner")
    monkeypatch.setattr(test_runner._SESSION, "post", never)
    return root


class TestSelfTargetReadsAreLocal:
    def test_files_come_from_the_committed_head_not_the_runner(self, self_repo):
        files, problems = test_runner.fetch_repo_files(
            "coding-model-server", ["src/pkg/mod.py"], "HEAD")
        assert files == [("src/pkg/mod.py", "VALUE = 1\n")]
        assert problems == []

    def test_a_directory_is_gits_tree_listing_like_the_runner(self, self_repo):
        files, problems = test_runner.fetch_repo_files(
            "coding-model-server", ["src/pkg/"], "HEAD")
        assert problems == []
        (path, content), = files
        assert path == "src/pkg/"
        assert content.startswith("tree HEAD:src/pkg/\n\n")
        assert "mod.py" in content and "other.py" in content

    def test_a_missing_path_is_a_per_path_problem_not_an_outage(self, self_repo):
        files, problems = test_runner.fetch_repo_files(
            "coding-model-server", ["src/pkg/mod.py", "tests/test_new.py"], "HEAD")
        assert [p for p, _ in files] == ["src/pkg/mod.py"]
        assert len(problems) == 1 and problems[0].startswith("tests/test_new.py: ")
        assert "does not exist" in problems[0]
        assert not test_runner.problems_indicate_runner_outage(
            problems, ["src/pkg/mod.py", "tests/test_new.py"])

    def test_unsafe_paths_are_refused_in_band(self, self_repo):
        files, problems = test_runner.fetch_repo_files(
            "coding-model-server", ["../etc/passwd", "/etc/passwd", ""], "HEAD")
        assert files == []
        assert problems == ["../etc/passwd: path escapes the repository",
                            "/etc/passwd: absolute paths are not accepted",
                            ": empty path"]

    def test_another_repo_still_goes_to_the_runner(self, self_repo, monkeypatch):
        calls = []

        class Resp:
            status_code = 200

            def json(self):
                return {"files": [{"path": "Sources/A.swift", "content": "x"}]}
        monkeypatch.setattr(test_runner._SESSION, "post",
                            lambda url, **k: calls.append((url, k)) or Resp())
        files, problems = test_runner.fetch_repo_files("centipede", ["Sources/A.swift"], "main")
        assert files == [("Sources/A.swift", "x")] and problems == []
        assert calls and calls[0][0].endswith("/v1/read_files")
        assert calls[0][1]["json"] == {"repo": "centipede", "base_ref": "main",
                                       "paths": ["Sources/A.swift"]}
