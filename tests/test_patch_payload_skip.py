"""DEV-196 — mac-runner patch payloads must not ship retry snapshots.

_clean_spec_dir_for_retry snapshots every prior attempt into
retry_history/retry_<N>/ before wiping. _collect_patch_files rglobbed the
whole spec dir, so a spec on retry N POSTed all N prior snapshots (payload
growing linearly) and materialized retry_history/ inside the Mac git
worktree — glob-based SPM targets then compiled N stale copies of every
source file alongside the current one.
"""
from coding_model_autonomous.executor import _collect_patch_files


def _make_spec_dir(tmp_path):
    spec = tmp_path / "spec_abc123"
    spec.mkdir()
    (spec / "Sources").mkdir()
    (spec / "Sources" / "App.swift").write_text("// current attempt\n")
    (spec / "Tests.swift").write_text("// tests\n")
    snap = spec / "retry_history" / "retry_0"
    snap.mkdir(parents=True)
    (snap / "Sources").mkdir()
    (snap / "Sources" / "App.swift").write_text("// stale attempt 0\n")
    (snap / "manifest.json").write_text("[]")
    return spec


def test_retry_history_is_not_shipped(tmp_path):
    spec = _make_spec_dir(tmp_path)

    patch_files, err = _collect_patch_files(spec)

    assert err is None
    paths = {p["path"] for p in patch_files}
    assert "Sources/App.swift" in paths
    assert "Tests.swift" in paths
    assert not any(p.startswith("retry_history/") for p in paths), (
        "prior-attempt snapshots must never reach the Mac worktree"
    )


def test_current_attempt_content_wins(tmp_path):
    # Belt and braces: even with identical relative names inside the
    # snapshot, only the live workspace copy ships.
    spec = _make_spec_dir(tmp_path)

    patch_files, _ = _collect_patch_files(spec)

    app = [p for p in patch_files if p["path"] == "Sources/App.swift"]
    assert len(app) == 1
    assert "current attempt" in app[0]["content"]
