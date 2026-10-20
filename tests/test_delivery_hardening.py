"""Delivery copies model-chosen paths out of a workspace the code under test
can write to, into a git clone, and pushes. These tests plant the two things
that makes dangerous — a path inside git's control directory, and a symlink
where an artifact should be — against a real local remote, and assert on the
outcome that matters: nothing executed, nothing copied, nothing pushed.
"""
import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from coding_model_autonomous import ArtifactKind, context, delivery
from coding_model_autonomous.workspace import (
    REFUSED_GIT_CONTROL, ArtifactLedger, artifact_path, is_git_control_path,
    symlink_in_path,
)
from coding_model_server.orchestrator_daemon import _record_tested_manifest


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


def _seed(tmp_path, populate=None):
    """A bare origin with one commit; *populate(seed_dir)* adds to it."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), "seed")
    (seed / "README.md").write_text("hello\n")
    if populate:
        populate(seed)
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    _git(seed, "push", "origin", "HEAD")
    return bare


@pytest.fixture
def remote(tmp_path, monkeypatch):
    bare = _seed(tmp_path)
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={bare}")
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY", raising=False)
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY_DEMO", raising=False)
    return bare


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec_ws"
    (d / "Sources").mkdir(parents=True)
    (d / "Sources" / "Thing.swift").write_text("struct Thing {}\n")
    return d


def _pipeline_branches(remote) -> str:
    return _git(remote, "for-each-ref", "refs/heads/pipeline/").strip()


def _fsmonitor_config(marker, origin) -> str:
    """A .git/config whose fsmonitor "hook" leaves *marker* behind. The
    trailing `#` swallows the arguments git appends to the command."""
    return ("[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            f'\tfsmonitor = "touch {marker} #"\n'
            f'[remote "origin"]\n\turl = {origin}\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n")


def _deliver(spec_dir, paths, **kw):
    return delivery.deliver_spec("spec_h1", "Hardening", spec_dir, paths,
                                 repo_name="demo", protected_paths=[], **kw)


# ── git's control directory ──────────────────────────────────────────────────

def test_a_git_config_artifact_is_refused_and_its_command_never_runs(
        remote, spec_dir, tmp_path):
    marker = tmp_path / "fsmonitor-ran"
    (spec_dir / ".git").mkdir()
    (spec_dir / ".git" / "config").write_text(_fsmonitor_config(marker, remote))

    r = _deliver(spec_dir, ["Sources/Thing.swift", ".git/config"])

    assert r.status == "failed"
    assert ".git/config" in r.detail and "control directory" in r.detail
    assert not marker.exists(), "the planted core.fsmonitor command executed"
    assert _pipeline_branches(remote) == "", "a refused delivery pushed"


def test_refusal_happens_before_any_git_command(remote, spec_dir, monkeypatch):
    (spec_dir / ".git").mkdir()
    (spec_dir / ".git" / "config").write_text("[core]\n")

    def no_git(*a, **k):
        raise AssertionError("an unsafe path must never reach git")

    monkeypatch.setattr(delivery, "_git", no_git)
    assert _deliver(spec_dir, [".git/config"]).status == "failed"


@pytest.mark.parametrize("rel", [
    ".GIT/config", ".Git/hooks/pre-commit", "sub/.git/config", ".git",
    ".git./config", ".git /config", "GIT~1/config", ".git\\config",
])
def test_every_spelling_of_the_control_directory_is_refused(
        remote, spec_dir, rel):
    assert is_git_control_path(rel)
    # Present on disk under a name this filesystem accepts, so the refusal is
    # the path's doing and not a missing file's.
    target = spec_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("[core]\n")

    r = _deliver(spec_dir, ["Sources/Thing.swift", rel])

    assert r.status == "failed" and rel in r.detail
    assert _pipeline_branches(remote) == ""


def test_ordinary_dot_git_named_files_still_deliver(remote, spec_dir, tmp_path):
    """Only the directory is control. These are tracked content a real change
    touches, and none of them can make git run anything in the clone."""
    files = {".github/workflows/ci.yml": "on: push\n",
             ".gitignore": "build/\n",
             ".gitattributes": "*.png binary\n",
             "docs/git/notes.md": "notes\n",
             "tools/.gitkeep": "\n"}
    for rel, text in files.items():
        (spec_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (spec_dir / rel).write_text(text)
        assert not is_git_control_path(rel)

    r = _deliver(spec_dir, list(files))

    assert r.status == "pushed", r.detail
    _git(tmp_path, "clone", "--branch", "pipeline/spec_h1", str(remote), "check")
    for rel, text in files.items():
        assert (tmp_path / "check" / rel).read_text() == text


def test_git_ignores_planted_config_even_if_a_path_check_is_missed(
        remote, spec_dir, tmp_path, monkeypatch):
    """The second lock. With the path checks switched off, the planted config
    DOES land in the clone — and the commands delivery runs there still do
    not execute what it names."""
    marker = tmp_path / "fsmonitor-ran"
    config = _fsmonitor_config(marker, remote)

    # Prove the bait is live on this git before trusting its silence.
    probe = tmp_path / "probe"
    _git(tmp_path, "init", str(probe))
    (probe / ".git" / "config").write_text(config)
    subprocess.run(["git", "status"], cwd=str(probe), capture_output=True)
    if not marker.exists():
        pytest.skip("this git does not run a core.fsmonitor hook command")
    marker.unlink()

    (spec_dir / ".git").mkdir()
    (spec_dir / ".git" / "config").write_text(config)
    monkeypatch.setattr(delivery, "unsafe_artifact_reason", lambda *a: None)
    monkeypatch.setattr(delivery, "_destination_refusal", lambda *a: None)

    _deliver(spec_dir, ["Sources/Thing.swift", ".git/config"])

    assert not marker.exists(), "git ran the planted core.fsmonitor command"


def test_a_planted_hook_does_not_run(remote, spec_dir, tmp_path, monkeypatch):
    """Same second lock, for hooks: one already sitting in the clone (here
    put there after the clone, standing in for a missed check) is not run by
    delivery's commit."""
    marker = tmp_path / "hook-ran"
    real_git = delivery._git

    def git_then_plant(cwd, *args, **kw):
        result = real_git(cwd, *args, **kw)
        if args[0] == "clone":
            hook = delivery.Path(str(cwd)) / "repo" / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
            hook.chmod(0o755)
        return result

    monkeypatch.setattr(delivery, "_git", git_then_plant)
    r = _deliver(spec_dir, ["Sources/Thing.swift"])

    assert r.status == "pushed", r.detail
    assert not marker.exists(), "git ran a hook from the clone"


# ── traversal ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", ["/etc/hostname", "../outside.txt",
                                 "Sources/../../outside.txt"])
def test_absolute_and_parent_paths_are_refused(remote, spec_dir, tmp_path, rel):
    (tmp_path / "outside.txt").write_text("not an artifact\n")

    r = _deliver(spec_dir, ["Sources/Thing.swift", rel])

    assert r.status == "failed" and rel in r.detail
    assert _pipeline_branches(remote) == ""


# ── symlinks in the workspace ────────────────────────────────────────────────

def test_a_symlinked_artifact_is_refused_and_its_target_never_leaves(
        remote, spec_dir, tmp_path):
    secret = tmp_path / "deploy_key"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (spec_dir / "Sources" / "Thing.swift").unlink()
    (spec_dir / "Sources" / "Thing.swift").symlink_to(secret)

    r = _deliver(spec_dir, ["Sources/Thing.swift"])

    assert r.status == "failed"
    assert "Sources/Thing.swift" in r.detail and "symbolic link" in r.detail
    assert "PRIVATE KEY" not in r.detail
    assert _pipeline_branches(remote) == "", "the link's target was pushed"


def test_a_symlinked_parent_directory_is_refused(remote, spec_dir, tmp_path):
    outside = tmp_path / "home_ssh"
    outside.mkdir()
    (outside / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (spec_dir / "keys").symlink_to(outside, target_is_directory=True)

    r = _deliver(spec_dir, ["Sources/Thing.swift", "keys/id_ed25519"])

    assert r.status == "failed"
    assert "keys/id_ed25519" in r.detail and "`keys`" in r.detail
    assert _pipeline_branches(remote) == ""


def test_symlink_in_path_names_the_link(spec_dir, tmp_path):
    (spec_dir / "a").mkdir()
    (spec_dir / "a" / "b").symlink_to(tmp_path, target_is_directory=True)
    assert symlink_in_path(spec_dir, "a/b/c.txt") == "a/b"
    assert symlink_in_path(spec_dir, "Sources/Thing.swift") is None
    assert symlink_in_path(spec_dir, "not/there.txt") is None


# ── symlinks in the clone ────────────────────────────────────────────────────

def test_a_tracked_link_into_the_control_directory_is_not_written_through(
        tmp_path, spec_dir, monkeypatch):
    """The target repository can hold a symlink too. `meta -> .git` makes
    the innocent path `meta/config` the clone's git config."""
    marker = tmp_path / "fsmonitor-ran"
    bare = _seed(tmp_path, lambda seed: (seed / "meta").symlink_to(".git"))
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={bare}")
    (spec_dir / "meta").mkdir()
    (spec_dir / "meta" / "config").write_text(_fsmonitor_config(marker, bare))

    r = _deliver(spec_dir, ["Sources/Thing.swift", "meta/config"])

    assert r.status == "failed"
    assert "meta/config" in r.detail and "control directory" in r.detail
    assert not marker.exists()
    assert _pipeline_branches(bare) == ""


def test_a_tracked_link_out_of_the_clone_is_not_written_through(
        tmp_path, spec_dir, monkeypatch):
    victim = tmp_path / "victim"
    victim.mkdir()
    bare = _seed(tmp_path, lambda seed: (seed / "out").symlink_to(victim))
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={bare}")
    (spec_dir / "out").mkdir()
    (spec_dir / "out" / "authorized_keys").write_text("ssh-ed25519 AAAA\n")

    r = _deliver(spec_dir, ["Sources/Thing.swift", "out/authorized_keys"])

    assert r.status == "failed" and "outside the clone" in r.detail
    assert list(victim.iterdir()) == [], "delivery wrote outside its clone"
    assert _pipeline_branches(bare) == ""


# ── git failures are failures ────────────────────────────────────────────────

def _git_failing_at(subcommand, rc):
    def fake_git(cwd, *args, timeout=120, key=""):
        if args[0] == "clone":
            (delivery.Path(str(cwd)) / "repo").mkdir()
        if args[0] == subcommand:
            return SimpleNamespace(returncode=rc, stdout="",
                                   stderr=f"fatal: {subcommand} broke")
        # "diff --cached --quiet" exits 1 when changes are staged.
        return SimpleNamespace(returncode=1 if args[0] == "diff" else 0,
                               stdout="", stderr="")
    return fake_git


@pytest.mark.parametrize("subcommand,rc", [("checkout", 128), ("add", 128),
                                           ("diff", 129)])
def test_a_failed_git_step_is_reported_as_a_failure(
        remote, spec_dir, monkeypatch, subcommand, rc):
    """A failed `add` leaves nothing staged, which `diff --cached --quiet`
    reports exactly as it reports "already delivered"."""
    calls: list = []
    fake = _git_failing_at(subcommand, rc)

    def recording(cwd, *args, **kw):
        calls.append(args[0])
        return fake(cwd, *args, **kw)

    monkeypatch.setattr(delivery, "_git", recording)
    r = _deliver(spec_dir, ["Sources/Thing.swift"])

    assert r.status == "failed", r.detail
    assert f"{subcommand} broke" in r.detail
    assert "push" not in calls and "commit" not in calls


# ── the tested manifest ──────────────────────────────────────────────────────

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_recorded_hashes_with_the_workspace_manifest_missing_refuses(tmp_path):
    (tmp_path / "a.py").write_bytes(b"code")
    ok, detail = delivery.verify_tested_manifest(
        tmp_path, ["a.py"], recorded={"a.py": _sha(b"code")})
    assert ok is False
    assert "REFUSED" in detail and "missing" in detail


def test_recorded_hashes_outrank_the_workspace_manifest(tmp_path):
    (tmp_path / "a.py").write_bytes(b"swapped")
    (tmp_path / delivery.TESTED_MANIFEST).write_text(
        json.dumps({"a.py": _sha(b"swapped")}))        # rewritten to agree
    ok, detail = delivery.verify_tested_manifest(
        tmp_path, ["a.py"], recorded={"a.py": _sha(b"code")})
    assert ok is False
    assert _sha(b"code") in detail and _sha(b"swapped") in detail


def test_recorded_hashes_that_match_pass_and_say_what_was_compared(tmp_path):
    (tmp_path / "a.py").write_bytes(b"code")
    (tmp_path / delivery.TESTED_MANIFEST).write_text("{}")   # not consulted
    ok, detail = delivery.verify_tested_manifest(
        tmp_path, ["a.py"], recorded={"a.py": _sha(b"code")})
    assert ok is True
    assert "1 file(s) verified against the build check's recorded hashes" in detail


def test_never_recorded_and_no_manifest_still_passes(tmp_path):
    """A spec with no build check has neither copy; it delivers as before."""
    (tmp_path / "a.py").write_bytes(b"code")
    assert delivery.verify_tested_manifest(
        tmp_path, ["a.py"], recorded=None) == (True, "")


@pytest.mark.parametrize("recorded", [None, {"a.py": _sha(b"code")}])
def test_a_symlinked_manifest_refuses(tmp_path, recorded):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"a.py": _sha(b"code")}))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_bytes(b"code")
    (ws / delivery.TESTED_MANIFEST).symlink_to(elsewhere)

    ok, detail = delivery.verify_tested_manifest(ws, ["a.py"], recorded=recorded)

    assert ok is False and "symbolic link" in detail


def test_verify_does_not_hash_through_a_symlinked_artifact(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"code")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").symlink_to(target)
    (ws / delivery.TESTED_MANIFEST).write_text(json.dumps({"a.py": _sha(b"code")}))

    ok, detail = delivery.verify_tested_manifest(ws, ["a.py"])

    assert ok is False and "symbolic link" in detail


def test_deliver_spec_refuses_when_the_recorded_manifest_was_deleted(
        remote, spec_dir):
    r = _deliver(spec_dir, ["Sources/Thing.swift"],
                 tested_hashes={"Sources/Thing.swift": _sha(b"struct Thing {}\n")})
    assert r.status == "failed" and "missing" in r.detail
    assert _pipeline_branches(remote) == ""


def test_recording_does_not_hash_through_a_symlink(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("daemon-readable\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.py").write_text("x = 1\n")
    (ws / "link.py").symlink_to(secret)
    payload: dict = {}

    _record_tested_manifest(ws, [("real.py", ""), ("link.py", "")], payload)

    assert payload["artifact_hashes"] == {"real.py": _sha(b"x = 1\n")}
    assert json.loads((ws / "tested_manifest.json").read_text()) == \
        payload["artifact_hashes"]


def test_recording_does_not_write_through_a_symlinked_manifest(tmp_path):
    """The manifest's own path can be the link: writing through it would let
    sandboxed code aim the daemon's write at any file the daemon can write."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.py").write_text("x = 1\n")
    (ws / "tested_manifest.json").symlink_to(victim)
    payload: dict = {}

    _record_tested_manifest(ws, [("real.py", "")], payload)

    assert victim.read_text() == "precious\n"
    manifest = ws / "tested_manifest.json"
    assert not manifest.is_symlink()
    assert json.loads(manifest.read_text()) == {"real.py": _sha(b"x = 1\n")}
    assert payload["artifact_hashes"] == {"real.py": _sha(b"x = 1\n")}


# ── the path never becomes an artifact ───────────────────────────────────────

@pytest.mark.parametrize("rel", [".git/config", ".GIT/config", "a/.git/HEAD",
                                 "/.git/config", ".git"])
def test_artifact_path_rejects_the_control_directory(tmp_path, rel):
    with pytest.raises(ValueError, match="git control path"):
        artifact_path(tmp_path, rel)


@pytest.mark.parametrize("rel", [".github/workflows/ci.yml", ".gitignore",
                                 ".gitattributes", ".gitmodules",
                                 "src/git/util.py", "docs/.gitkeep"])
def test_artifact_path_accepts_ordinary_git_adjacent_names(tmp_path, rel):
    assert artifact_path(tmp_path, rel) == (tmp_path / rel).resolve()


def test_artifact_path_rejects_a_link_that_resolves_into_dot_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "meta").symlink_to(".git")
    with pytest.raises(ValueError, match="git control path"):
        artifact_path(tmp_path, "meta/config")


def test_the_ledger_refuses_a_git_config_write_without_raising(tmp_path):
    ledger = ArtifactLedger(None, "spec_x", tmp_path)

    out = ledger.write(".git/config", "[core]\n\tfsmonitor = evil\n",
                       role="implementer", kind=ArtifactKind.CODE)

    assert out.action == REFUSED_GIT_CONTROL and out.refused and not out.ok
    assert ".git/config" in out.describe()
    assert not (tmp_path / ".git").exists()
    # A legitimate neighbour is unaffected.
    ok = ledger.write(".github/workflows/ci.yml", "on: push\n",
                      role="implementer", kind=ArtifactKind.CODE)
    assert ok.ok and (tmp_path / ".github/workflows/ci.yml").is_file()


# ── the reviewer's prompt ────────────────────────────────────────────────────

class _Artifacts:
    def __init__(self, *paths):
        self.rows = [SimpleNamespace(path=p, role="implementer") for p in paths]

    def list_artifacts(self, spec_id, kind=None):
        return self.rows


def test_reviewer_context_does_not_read_through_a_symlink(tmp_path):
    secret = tmp_path / "environ"
    secret.write_text("ADMIN_API_KEY=hunter2\n")
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "real.py").write_text("x = 1\n")
    (ws / "leak.py").symlink_to(secret)
    os.symlink(tmp_path, ws / "pkg" / "up")

    shown = dict(context.prior_artifacts(
        _Artifacts("real.py", "leak.py", "pkg/up/environ"), "spec_x", ws))

    assert shown["real.py"] == "x = 1\n"
    assert "hunter2" not in "".join(shown.values())
    assert "symbolic link" in shown["leak.py"]
    assert "symbolic link" in shown["pkg/up/environ"]
