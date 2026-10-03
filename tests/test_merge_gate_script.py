"""scripts/merge_gate.sh, end to end against a throwaway repository.

The gate used to check out main and hold the uncommitted merge in the live
checkout while the checks ran, and had no trap: an interrupted gate left the
checkout the services import half-merged. It now merges and checks in a
temporary worktree, and the live checkout only ever fast-forwards to a commit
that already passed.

Each test builds its own repository under tmp_path with a stub
``venv/bin/python`` standing in for ruff, mypy and pytest. The stub goes red
on marker files in its working directory, so a red result also proves the
checks ran on the merged tree: the markers exist only on the branch.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_gate.sh"

STUB_PYTHON = r"""#!/usr/bin/env bash
printf '%s|%s|%s\n' "$(pwd -P)" "${PYTHONPATH:-}" "$*" >> "$GATE_STUB_LOG"
case "$1 $2" in
  "-c "*)
    if [ -n "${GATE_STUB_IMPORT:-}" ]; then
      echo "$GATE_STUB_IMPORT"
    else
      echo "${PYTHONPATH%%:*}/coding_model_autonomous/__init__.py"
    fi ;;
  "-m ruff") [ ! -e ruff_red.marker ] ;;
  "-m mypy") [ ! -e mypy_red.marker ] ;;
  "-m pytest")
    if [ -n "${GATE_STUB_HOOK:-}" ]; then bash -c "$GATE_STUB_HOOK"; fi
    if [ -n "${GATE_STUB_HANG:-}" ]; then
      : > "$GATE_STUB_HANG"
      exec sleep 60
    fi
    if [ -e pytest_red.marker ]; then echo "1 failed, 2 passed in 0.01s"; exit 1; fi
    echo "3 passed in 0.01s" ;;
  *) echo "stub python: unexpected arguments: $*" >&2; exit 97 ;;
esac
"""


class Repo:
    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "live"
        self.tmp = tmp_path / "tmp"
        self.stub_log = tmp_path / "stub.log"
        self.tmp.mkdir()
        self.path.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            TMPDIR=str(self.tmp), GATE_STUB_LOG=str(self.stub_log),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.env = env

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run(["git", *args], cwd=cwd or self.path, env=self.env,
                              capture_output=True, text=True)
        assert done.returncode == 0, f"git {args}: {done.stderr}"
        return done.stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        for rel, text in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.git("add", "--", *files)
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str, files: dict[str, str]) -> str:
        """Commit `files` on a new branch off main, and return to main."""
        self.git("checkout", "--quiet", "-b", name, "main")
        sha = self.commit(files, f"work on {name}")
        self.git("checkout", "--quiet", "main")
        return sha

    def gate(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=self.path,
                              env={**self.env, **env}, capture_output=True,
                              text=True, timeout=60)

    def snapshot(self) -> dict:
        """Everything the gate must not disturb: every file's bytes (git's own
        directory aside), what git thinks is modified, and where the refs are."""
        files = {
            str(p.relative_to(self.path)): p.read_bytes()
            for p in sorted(self.path.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(self.path).parts}
        return {"files": files,
                "status": self.git("status", "--porcelain"),
                "head": self.git("rev-parse", "HEAD"),
                "on": self.git("branch", "--show-current"),
                "main": self.git("rev-parse", "refs/heads/main")}

    def stub_calls(self) -> list[tuple[str, str, str]]:
        if not self.stub_log.exists():
            return []
        return [tuple(line.split("|", 2))  # type: ignore[misc]
                for line in self.stub_log.read_text().splitlines()]

    def assert_no_worktree_left(self) -> None:
        assert self.git("worktree", "list", "--porcelain").count("worktree ") == 1
        assert list((self.tmp / "merge_gate").glob("wt.*")) == []


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path)
    r.git("init", "--quiet", "-b", "main")
    r.commit({"src/coding_model_autonomous/__init__.py": "",
              "app.py": "print('v1')\n",
              "notes.md": "notes\n"}, "base")
    # Untracked in the live checkout, as the real venv is.
    python = r.path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(STUB_PYTHON)
    python.chmod(0o755)
    return r


def test_green_gate_fast_forwards_main_to_the_gated_merge_commit(repo):
    old_main = repo.git("rev-parse", "main")
    tip = repo.branch("feature", {"app.py": "print('v2')\n", "new.py": "x = 1\n"})

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "GATE ok=1 ruff=0 mypy=0 pytest=0 branch=feature" in done.stdout
    assert "  pytest: 3 passed in 0.01s" in done.stdout
    assert "MERGED:" in done.stdout
    assert repo.git("rev-list", "--parents", "-1", "main").split()[1:] == [old_main, tip]
    assert repo.git("log", "-1", "--format=%s", "main") == "Merge branch 'feature'"
    # The live checkout is on the new main, with the merged files and nothing
    # git considers modified (the stub venv stays untracked).
    assert repo.git("branch", "--show-current") == "main"
    assert (repo.path / "app.py").read_text() == "print('v2')\n"
    assert (repo.path / "new.py").read_text() == "x = 1\n"
    assert repo.git("status", "--porcelain", "--untracked-files=no") == ""
    repo.assert_no_worktree_left()


def test_checks_run_in_the_temporary_worktree_and_import_it(repo):
    repo.branch("feature", {"new.py": "x = 1\n"})

    assert repo.gate("--no-push", "feature").returncode == 0

    calls = repo.stub_calls()
    assert [c[2].split()[1] for c in calls[1:]] == ["ruff", "mypy", "pytest"]
    assert calls[3][2] == "-m pytest -q -m not network -n 8"
    gate_dir = (repo.tmp / "merge_gate").resolve()
    for cwd, pythonpath, _ in calls:
        # A directory of its own per run, holding a worktree that keeps the
        # checkout's name.
        assert Path(cwd).parent.parent == gate_dir
        assert Path(cwd).parent.name.startswith("wt.")
        assert Path(cwd).name == repo.path.name
        assert pythonpath == f"{cwd}/src:{cwd}"


@pytest.mark.parametrize("check", ["ruff", "mypy", "pytest"])
def test_red_gate_leaves_main_and_the_live_tree_byte_identical(repo, check):
    repo.branch("feature", {"app.py": "print('v2')\n", f"{check}_red.marker": ""})
    before = repo.snapshot()

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 1, done.stdout + done.stderr
    codes = {name: int(name == check) for name in ("ruff", "mypy", "pytest")}
    assert ("GATE ok=0 ruff={ruff} mypy={mypy} pytest={pytest} branch=feature"
            .format(**codes)) in done.stdout
    assert "NOT MERGED: gate red" in done.stdout
    assert repo.snapshot() == before
    repo.assert_no_worktree_left()


def test_dry_run_reports_green_and_moves_nothing(repo):
    repo.branch("feature", {"app.py": "print('v2')\n"})
    before = repo.snapshot()

    done = repo.gate("--dry-run", "feature")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "GATE ok=1" in done.stdout and "DRY RUN" in done.stdout
    assert repo.snapshot() == before
    repo.assert_no_worktree_left()


def test_local_changes_the_merge_does_not_touch_do_not_block_and_survive(repo):
    repo.branch("feature", {"app.py": "print('v2')\n"})
    (repo.path / "scratch.txt").write_text("untracked\n")
    (repo.path / "notes.md").write_text("edited, uncommitted\n")

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (repo.path / "app.py").read_text() == "print('v2')\n"
    assert (repo.path / "scratch.txt").read_text() == "untracked\n"
    assert (repo.path / "notes.md").read_text() == "edited, uncommitted\n"
    # The uncommitted edit is not in the merge commit either.
    assert repo.git("show", "main:notes.md") == "notes"


@pytest.mark.parametrize("staged", [False, True])
def test_tracked_change_the_merge_would_overwrite_refuses_before_any_check(repo, staged):
    repo.branch("feature", {"app.py": "print('v2')\n"})
    (repo.path / "app.py").write_text("print('local edit')\n")
    if staged:
        repo.git("add", "app.py")
    before = repo.snapshot()

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 2, done.stdout + done.stderr
    assert "refused" in done.stderr and "app.py" in done.stderr
    assert "GATE" not in done.stdout
    assert repo.stub_calls() == []
    assert repo.snapshot() == before
    repo.assert_no_worktree_left()


def test_untracked_file_the_merge_would_create_refuses(repo):
    repo.branch("feature", {"new.py": "x = 1\n"})
    (repo.path / "new.py").write_text("mine\n")
    before = repo.snapshot()

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 2
    assert "new.py" in done.stderr
    assert repo.snapshot() == before


def test_collision_that_appears_during_the_gate_parks_the_gated_commit(repo):
    """The fast-forward is refused by git itself, whole: the local edit stays,
    main stays, and the commit that passed is kept under a ref so the merge
    can be finished without running the gate again."""
    old_main = repo.git("rev-parse", "main")
    tip = repo.branch("feature", {"app.py": "print('v2')\n"})
    edit = f"echo \"print('edited mid-gate')\" > '{repo.path}/app.py'"

    done = repo.gate("--no-push", "feature", GATE_STUB_HOOK=edit)

    assert done.returncode == 2, done.stdout + done.stderr
    assert "GATE ok=1" in done.stdout
    assert "NOT MERGED" in done.stderr
    assert "git merge --ff-only refs/merge-gate/feature" in done.stderr
    assert repo.git("rev-parse", "main") == old_main
    assert (repo.path / "app.py").read_text() == "print('edited mid-gate')\n"
    assert repo.git("status", "--porcelain", "--untracked-files=no") == "M app.py"
    repo.assert_no_worktree_left()

    parked = repo.git("rev-parse", "refs/merge-gate/feature")
    assert repo.git("rev-list", "--parents", "-1", parked).split()[1:] == [old_main, tip]
    repo.git("checkout", "--", "app.py")
    repo.git("merge", "--ff-only", "refs/merge-gate/feature")
    assert repo.git("rev-parse", "main") == parked


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signal_mid_gate_leaves_the_live_tree_clean_and_no_worktree(repo, tmp_path, sig):
    repo.branch("feature", {"app.py": "print('v2')\n"})
    before = repo.snapshot()
    started = tmp_path / "pytest.started"

    # SIGINT is reset to its default in the child: a shell cannot trap a
    # signal it inherited as ignored, and this suite may itself be running
    # under something that ignores it.
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "--no-push", "feature"], cwd=repo.path,
        env={**repo.env, "GATE_STUB_HANG": str(started)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    try:
        deadline = time.monotonic() + 30
        while not started.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "the gate never reached pytest"
            time.sleep(0.02)
        # Mid-gate there is a second worktree; the live one is already as it
        # will be left.
        assert repo.git("worktree", "list", "--porcelain").count("worktree ") == 2
        assert repo.snapshot() == before
        proc.send_signal(sig)
        # Well inside the stub's 60 s sleep: the gate does not wait for the
        # running check before it cleans up.
        out, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    assert proc.returncode == 128 + sig
    assert "GATE" not in out
    assert repo.snapshot() == before
    repo.assert_no_worktree_left()


def test_main_moved_during_the_gate_refuses_and_keeps_the_newer_main(repo):
    repo.branch("feature", {"app.py": "print('v2')\n"})
    move = (f"git -C '{repo.path}' commit --quiet --allow-empty "
            "-m 'landed while the gate ran'")

    done = repo.gate("--no-push", "feature", GATE_STUB_HOOK=move)

    assert done.returncode == 2, done.stdout + done.stderr
    assert "main moved during the gate" in done.stderr
    assert repo.git("log", "-1", "--format=%s", "main") == "landed while the gate ran"
    assert (repo.path / "app.py").read_text() == "print('v1')\n"
    assert repo.git("status", "--porcelain", "--untracked-files=no") == ""
    repo.assert_no_worktree_left()


def test_with_another_branch_checked_out_only_the_main_ref_moves(repo):
    old_main = repo.git("rev-parse", "main")
    tip = repo.branch("feature", {"app.py": "print('v2')\n"})
    repo.git("checkout", "--quiet", "-b", "elsewhere")
    (repo.path / "app.py").write_text("print('wip on elsewhere')\n")

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 0, done.stdout + done.stderr
    assert repo.git("rev-list", "--parents", "-1", "main").split()[1:] == [old_main, tip]
    # The checkout follows `elsewhere`, so nothing in it changed, not even the
    # uncommitted edit to a file the merge rewrites.
    assert repo.git("branch", "--show-current") == "elsewhere"
    assert repo.git("rev-parse", "HEAD") == old_main
    assert (repo.path / "app.py").read_text() == "print('wip on elsewhere')\n"


def test_main_ref_moved_under_another_branch_is_not_overwritten(repo):
    repo.branch("feature", {"app.py": "print('v2')\n"})
    other = repo.branch("other", {"other.py": "y = 2\n"})
    repo.git("checkout", "--quiet", "-b", "elsewhere")
    move = f"git -C '{repo.path}' update-ref refs/heads/main {other}"

    done = repo.gate("--no-push", "feature", GATE_STUB_HOOK=move)

    assert done.returncode == 2, done.stdout + done.stderr
    assert "main moved during the gate" in done.stderr
    assert repo.git("rev-parse", "main") == other


def test_main_checked_out_in_another_worktree_refuses(repo, tmp_path):
    """Moving the ref from here would leave that worktree's files behind its
    own HEAD, which reads there as a pile of uncommitted reverts."""
    repo.branch("feature", {"app.py": "print('v2')\n"})
    repo.git("checkout", "--quiet", "-b", "elsewhere")
    other = tmp_path / "other-checkout"
    repo.git("worktree", "add", "--quiet", str(other), "main")
    old_main = repo.git("rev-parse", "main")

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 2
    assert "checked out in another worktree" in done.stderr
    assert repo.stub_calls() == []
    assert repo.git("rev-parse", "main") == old_main
    assert repo.git("status", "--porcelain", cwd=other) == ""


def test_refuses_when_the_checks_would_import_the_live_checkout(repo):
    """Fail closed: a green result from checks that imported main-before-the-
    merge says nothing about the merge."""
    repo.branch("feature", {"app.py": "print('v2')\n"})
    before = repo.snapshot()
    live = str(repo.path / "src" / "coding_model_autonomous" / "__init__.py")

    done = repo.gate("--no-push", "feature", GATE_STUB_IMPORT=live)

    assert done.returncode == 2
    assert "would not import the merged tree" in done.stderr
    assert [c[2].split()[0] for c in repo.stub_calls()] == ["-c"]
    assert repo.snapshot() == before
    repo.assert_no_worktree_left()


def test_conflicting_branch_refuses_without_touching_the_live_tree(repo):
    repo.branch("feature", {"app.py": "print('theirs')\n"})
    repo.commit({"app.py": "print('ours')\n"}, "main moves on")
    before = repo.snapshot()

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 2
    assert "does not apply cleanly" in done.stderr
    assert repo.snapshot() == before
    repo.assert_no_worktree_left()


def test_worktree_left_by_a_killed_gate_is_reaped_by_the_next_run(repo):
    """SIGKILL skips the trap. The leftover must neither block the next gate
    nor accumulate."""
    repo.branch("feature", {"app.py": "print('v2')\n"})
    gate_dir = repo.tmp / "merge_gate"
    gate_dir.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    leftover = gate_dir.resolve() / f"wt.{dead.pid}.abc123" / repo.path.name
    leftover.parent.mkdir()
    repo.git("worktree", "add", "--quiet", "--detach", str(leftover), "main")
    repo.git("merge", "--no-ff", "--no-commit", "--quiet", "feature", cwd=leftover)

    done = repo.gate("--no-push", "feature")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not leftover.parent.exists()
    repo.assert_no_worktree_left()


def test_green_gate_pushes_main_unless_told_not_to(repo, tmp_path):
    origin = tmp_path / "origin.git"
    repo.git("init", "--quiet", "--bare", "-b", "main", str(origin))
    repo.git("remote", "add", "origin", str(origin))
    repo.git("push", "--quiet", "origin", "main")
    repo.branch("feature", {"app.py": "print('v2')\n"})

    done = repo.gate("feature")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "PUSHED origin/main" in done.stdout
    assert repo.git("rev-parse", "main", cwd=origin) == repo.git("rev-parse", "main")


def test_live_only_state_the_suite_reads_is_visible_to_pytest(repo, tmp_path):
    """The spec archive is untracked, and the replay tests read it relative to
    the working directory. The temporary worktree has to offer it, or those
    tests would skip and the gate would be weaker than the one it replaces."""
    repo.branch("feature", {"app.py": "print('v2')\n"})
    design = repo.path / "var" / "tasks_db" / "specs" / "spec_1" / "design.md"
    design.parent.mkdir(parents=True)
    design.write_text("archived design\n")
    seen = tmp_path / "seen.txt"

    done = repo.gate("--no-push", "feature",
                     GATE_STUB_HOOK=f"cat var/tasks_db/specs/spec_1/design.md > '{seen}'")

    assert done.returncode == 0, done.stdout + done.stderr
    assert seen.read_text() == "archived design\n"
    # Removing the worktree must not follow the link into the archive.
    assert design.read_text() == "archived design\n"
    repo.assert_no_worktree_left()
