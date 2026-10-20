"""Delivery, driven end to end: the real daemon, a real local git remote.

The rest of the seam tier runs with no delivery remote, so delivery is a
skip that never touches git. These tests give the fixture spec a bare
repository to deliver to and assert on what actually arrives there.
"""
from __future__ import annotations

import subprocess

import pytest

from coding_model_autonomous import EventKind, GateType, SpecStatus

from seam_fakes import Reply
from seam_harness import (
    DAEMON_PATH, approve_all, drive, events, implementer_reply,
    make_executing_spec, approve_design, reviewer_reply, wait_at,
)

BRANCH_REF = "refs/heads/pipeline/{}"


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare origin with one commit, configured as the fixture spec's
    delivery remote."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), "seed")
    seed = tmp_path / "seed"
    (seed / "README.md").write_text("hello\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    _git(seed, "push", "origin", "HEAD")
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES",
                       f"coding-model-server={bare}")
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY", raising=False)
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY_CODING_MODEL_SERVER",
                       raising=False)
    return bare


def pushed_branch(remote, spec_id: str) -> bool:
    return bool(_git(remote, "for-each-ref", BRANCH_REF.format(spec_id)).strip())


def to_release_gate(db, model, runner):
    """A spec driven up to — not through — its release gate."""
    spec = make_executing_spec(db)
    approve_design(db, spec)
    model.script("implementer", Reply(implementer_reply()))
    model.script("reviewer", Reply(reviewer_reply("PASS")))
    out = drive(db, spec.id, model, wait_at(GateType.RELEASE_APPROVAL),
                runner=runner)
    assert out.reason == "waiting", out.reason
    return spec


def delivery_events(db, spec_id: str) -> list[dict]:
    return events(db, spec_id, EventKind.AGENT_RAN, role="delivery")


class TestManifestFromTheDatabase:
    def test_a_clean_run_is_verified_against_the_recorded_hashes(
            self, db, model, runner, remote):
        spec = to_release_gate(db, model, runner)

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE
        delivered = delivery_events(db, spec.id)
        assert [e["status"] for e in delivered] == ["pushed"], delivered
        assert "verified against the build check's recorded hashes" in (
            delivered[0]["detail"])
        assert pushed_branch(remote, spec.id)

    def test_deleting_the_workspace_manifest_does_not_unlock_delivery(
            self, db, model, runner, remote):
        """The tests run with the workspace writable. Code under test that
        swaps an artifact and deletes tested_manifest.json used to find the
        compare step simply switched off."""
        spec = to_release_gate(db, model, runner)
        spec_dir = db.spec_dir(spec.id)
        (spec_dir / "tested_manifest.json").unlink()
        (spec_dir / DAEMON_PATH).write_text("swapped = True\n")

        out = drive(db, spec.id, model, approve_all, runner=runner)

        assert out.status == SpecStatus.DONE      # fail-open: still DONE
        delivered = delivery_events(db, spec.id)
        assert [e["status"] for e in delivered] == ["failed"]
        assert "REFUSED" in delivered[0]["detail"]
        assert not pushed_branch(remote, spec.id)

    def test_a_rewritten_workspace_manifest_is_not_believed(
            self, db, model, runner, remote):
        """Rewriting the manifest to match a swapped artifact fools a
        verifier that reads the workspace copy, and no other."""
        import hashlib
        import json
        spec = to_release_gate(db, model, runner)
        spec_dir = db.spec_dir(spec.id)
        (spec_dir / DAEMON_PATH).write_text("swapped = True\n")
        manifest = json.loads((spec_dir / "tested_manifest.json").read_text())
        manifest[DAEMON_PATH] = hashlib.sha256(b"swapped = True\n").hexdigest()
        (spec_dir / "tested_manifest.json").write_text(json.dumps(manifest))

        drive(db, spec.id, model, approve_all, runner=runner)

        delivered = delivery_events(db, spec.id)
        assert [e["status"] for e in delivered] == ["failed"]
        assert "divergence" in delivered[0]["detail"]
        assert DAEMON_PATH in (spec_dir / "delivery_report.md").read_text()
        assert not pushed_branch(remote, spec.id)
