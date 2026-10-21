"""The dashboard's video-pipeline route reads the pipeline's JSON snapshot (DEV-401)."""
import json
import os
import time

from coding_model_server.routes import video_pipeline


def _write(tmp_path, **doc):
    (tmp_path / "state.json").write_text(json.dumps(doc))


def test_no_state_file_is_unavailable_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PIPELINE_STATE_DIR", str(tmp_path))
    out = video_pipeline.admin_video_pipeline()
    assert out["available"] is False and "no state file" in out["reason"]


def test_corrupt_state_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PIPELINE_STATE_DIR", str(tmp_path))
    (tmp_path / "state.json").write_text("{not json")
    assert video_pipeline.admin_video_pipeline()["available"] is False


def test_unfinished_run_with_dead_pid_is_not_active(tmp_path, monkeypatch):
    # A SIGKILLed run leaves finished_at null forever; the process table, not
    # the file, decides whether it is still running.
    monkeypatch.setenv("VIDEO_PIPELINE_STATE_DIR", str(tmp_path))
    _write(tmp_path, pid=2**22 + 1, finished_at=None, updated_at=time.time() - 90, jobs=[])
    out = video_pipeline.admin_video_pipeline()
    assert out["available"] is True and out["active"] is False
    assert out["stale_seconds"] >= 90


def test_active_requires_a_pipeline_process(tmp_path, monkeypatch):
    # Our own pid is alive but is pytest, not video-pipeline: a recycled pid
    # must not make a dead run look live.
    monkeypatch.setenv("VIDEO_PIPELINE_STATE_DIR", str(tmp_path))
    _write(tmp_path, pid=os.getpid(), finished_at=None, updated_at=time.time(), jobs=[])
    assert video_pipeline.admin_video_pipeline()["active"] is False


def test_live_pipeline_process_is_active(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PIPELINE_STATE_DIR", str(tmp_path))
    _write(tmp_path, pid=4242, finished_at=None, updated_at=time.time(), jobs=[])
    monkeypatch.setattr(video_pipeline, "_alive", lambda pid: pid == 4242)
    assert video_pipeline.admin_video_pipeline()["active"] is True


def test_finished_run_is_never_active(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PIPELINE_STATE_DIR", str(tmp_path))
    _write(tmp_path, pid=4242, finished_at=time.time(), updated_at=time.time(), jobs=[])
    monkeypatch.setattr(video_pipeline, "_alive", lambda pid: True)
    assert video_pipeline.admin_video_pipeline()["active"] is False
