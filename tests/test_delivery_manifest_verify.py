"""DEV-602 split C: delivery verifies the shipping set against the tested manifest.

Run 17 delivered files that were not the files the build check had tested;
delivery compared nothing. Split B records `tested_manifest.json` at the
passing pre-gate check; this verifies the shipping set against it before
anything touches a remote. An absent manifest keeps delivery byte-identical
to the pre-split-B behavior.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

import coding_model_autonomous.delivery as dv


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _workspace(tmp_path, files: dict, manifest: "dict | None" = None,
               raw_manifest: "str | None" = None):
    for rel, data in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if manifest is not None:
        (tmp_path / dv.TESTED_MANIFEST).write_text(json.dumps(manifest))
    if raw_manifest is not None:
        (tmp_path / dv.TESTED_MANIFEST).write_text(raw_manifest)
    return tmp_path


# ── the compare step (no git anywhere) ───────────────────────────────────────

def test_c3_absent_manifest_passes_through_silently(tmp_path):
    _workspace(tmp_path, {"a.py": b"code"})
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py"])
    assert ok is True
    assert detail == ""  # nothing appended → today's report, byte-identical


def test_c2_exact_match_passes_and_counts(tmp_path):
    files = {"a.py": b"code-a", "sub/b.py": b"code-b"}
    _workspace(tmp_path, files,
               manifest={rel: _sha(data) for rel, data in files.items()})
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py", "sub/b.py"])
    assert ok is True
    assert "2 file(s) verified against tested_manifest.json" in detail


def test_c1_mutated_file_refuses_with_both_hashes(tmp_path):
    tested_bytes, shipped_bytes = b"what the check saw", b"what would ship"
    _workspace(tmp_path, {"a.py": shipped_bytes},
               manifest={"a.py": _sha(tested_bytes)})
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py"])
    assert ok is False
    assert "REFUSED" in detail and "a.py" in detail
    assert _sha(tested_bytes) in detail and _sha(shipped_bytes) in detail


def test_c4_manifest_path_missing_from_shipping_set(tmp_path):
    _workspace(tmp_path, {"a.py": b"code"},
               manifest={"a.py": _sha(b"code"), "gone.py": _sha(b"tested")})
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py"])
    assert ok is False
    assert "gone.py" in detail and "shipped missing" in detail


def test_c4_shipped_file_absent_from_manifest(tmp_path):
    _workspace(tmp_path, {"a.py": b"code", "extra.py": b"sneaked in"},
               manifest={"a.py": _sha(b"code")})
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py", "extra.py"])
    assert ok is False
    assert "extra.py" in detail and "tested missing" in detail


def test_every_divergent_path_is_listed(tmp_path):
    _workspace(tmp_path, {"a.py": b"drifted", "extra.py": b"new"},
               manifest={"a.py": _sha(b"original"), "gone.py": _sha(b"x")})
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py", "extra.py"])
    assert ok is False
    for rel in ("a.py", "extra.py", "gone.py"):
        assert rel in detail


def test_unreadable_manifest_refuses(tmp_path):
    _workspace(tmp_path, {"a.py": b"code"}, raw_manifest="{not json")
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py"])
    assert ok is False and "REFUSED" in detail


def test_non_dict_manifest_refuses(tmp_path):
    _workspace(tmp_path, {"a.py": b"code"}, raw_manifest='["a.py"]')
    ok, detail = dv.verify_tested_manifest(tmp_path, ["a.py"])
    assert ok is False and "REFUSED" in detail


# ── wiring inside deliver_spec (git fully stubbed; no remote, no network) ────

@pytest.fixture
def remote_env(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES",
                       "selftest=git@example.invalid:me/selftest.git")
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY", raising=False)
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY_SELFTEST", raising=False)


def _stub_git(calls):
    def fake_git(cwd, *args, timeout=120, key=""):
        calls.append(args)
        if args and args[0] == "clone":
            (dv.Path(str(cwd)) / "repo").mkdir()
        # "diff --cached --quiet" exits 1 when changes are staged.
        rc = 1 if args and args[0] == "diff" else 0
        return SimpleNamespace(returncode=rc, stderr="")
    return fake_git


def test_divergence_refuses_before_any_git_command(tmp_path, remote_env,
                                                   monkeypatch):
    _workspace(tmp_path, {"a.py": b"shipped"},
               manifest={"a.py": _sha(b"tested")})

    def no_git(*a, **k):
        raise AssertionError("a divergent set must never reach git")

    monkeypatch.setattr(dv, "_git", no_git)
    result = dv.deliver_spec("spec_t", "t", tmp_path, ["a.py"],
                             repo_name="selftest", protected_paths=[])
    assert result.status == "failed"
    assert "REFUSED" in result.detail and "a.py" in result.detail


def test_matching_manifest_pushes_and_notes_verified_count(tmp_path,
                                                           remote_env,
                                                           monkeypatch):
    _workspace(tmp_path, {"a.py": b"code"}, manifest={"a.py": _sha(b"code")})
    calls: list = []
    monkeypatch.setattr(dv, "_git", _stub_git(calls))
    result = dv.deliver_spec("spec_t", "t", tmp_path, ["a.py"],
                             repo_name="selftest", protected_paths=[])
    assert result.status == "pushed"
    assert "1 file(s) verified against tested_manifest.json" in result.detail
    assert any(args and args[0] == "push" for args in calls)


def test_no_manifest_keeps_todays_pushed_detail(tmp_path, remote_env,
                                                monkeypatch):
    _workspace(tmp_path, {"a.py": b"code"})
    calls: list = []
    monkeypatch.setattr(dv, "_git", _stub_git(calls))
    result = dv.deliver_spec("spec_t", "t", tmp_path, ["a.py"],
                             repo_name="selftest", protected_paths=[])
    assert result.status == "pushed"
    assert "verified against" not in result.detail  # C3: byte-identical
