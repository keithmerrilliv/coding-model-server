"""DEV-790: a single-call retry is served the previous attempt's new files."""
import coding_model_server.orchestrator_daemon as d


def _snap(tmp_path, n, files):
    snap = tmp_path / "retry_history" / f"retry_{n}"
    for rel, content in files.items():
        fp = snap / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)


def test_retry_zero_serves_nothing(tmp_path):
    _snap(tmp_path, 0, {"Tests/A.swift": "x"})
    served, new = d._prior_new_files_as_editable(tmp_path, 0, ["Tests/A.swift"])
    assert served == [] and new == ["Tests/A.swift"]


def test_retry_serves_the_newest_snapshot_and_keeps_unseen_paths_new(tmp_path):
    _snap(tmp_path, 0, {"Tests/A.swift": "old", "Sources/B.swift": "b0"})
    _snap(tmp_path, 1, {"Tests/A.swift": "newer"})
    served, new = d._prior_new_files_as_editable(
        tmp_path, 2, ["Tests/A.swift", "Sources/B.swift", "Sources/C.swift"])
    assert served == [("Tests/A.swift", "newer"), ("Sources/B.swift", "b0")]
    assert new == ["Sources/C.swift"]


def test_no_snapshot_dir_leaves_everything_new(tmp_path):
    served, new = d._prior_new_files_as_editable(tmp_path, 1, ["Tests/A.swift"])
    assert served == [] and new == ["Tests/A.swift"]
