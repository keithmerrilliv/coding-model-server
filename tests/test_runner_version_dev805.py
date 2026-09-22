"""DEV-805: the runner reports what code it is serving, and the caller says so.

`mac_runner/*` deploys on the MacBook and nowhere else. A fix merged on the
orchestrator host is inert there until somebody pulls, and twice that has been
invisible from this side: DEV-705's timeout raise sat inert for a day while the
record called it fixed, and DEV-752's whole runner-side half had no way to be
confirmed from the caller.
"""
import logging
import subprocess

import pytest
import requests
from fastapi.testclient import TestClient

from mac_runner import server
from mac_runner.config import Config
from coding_model_autonomous import test_runner


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "test-key")
    monkeypatch.setattr(Config, "REPOS_FILE", tmp_path / "repos.yml")
    return TestClient(server.app)


# ── the endpoint ─────────────────────────────────────────────────────────────

def test_version_needs_the_key(client):
    """/health stayed liveness-only for a reason (DEV-170): an unauthenticated
    :5050 leaked project codenames, and a commit of a private repo is the same
    class of disclosure."""
    assert client.get("/v1/version").status_code == 401
    assert client.get("/health").json() == {"status": "ok"}
    assert "commit" not in client.get("/health").json()


def test_version_reports_the_commit_and_the_timeout_table(client):
    body = client.get("/v1/version", headers={"X-Runner-Key": "test-key"}).json()
    assert set(body) == {"commit", "dirty", "timeouts"}
    # This checkout is a git repo, so the runner can answer honestly here.
    head = subprocess.run(["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert body["commit"] == head
    assert body["timeouts"]["xcodebuild_test"] == 1200


def test_version_abstains_rather_than_guessing(monkeypatch, client):
    """A deployment need not be a git checkout; a guessed version is worse
    than an absent one."""
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    body = client.get("/v1/version", headers={"X-Runner-Key": "test-key"}).json()
    assert body["commit"] is None and body["dirty"] is None


# ── the caller's use of it ───────────────────────────────────────────────────

def test_disagreeing_timeout_tables_are_warned_about(monkeypatch, caplog):
    """The effective budget is the minimum of the two, so a one-sided raise is
    inert — the exact shape of DEV-705."""
    monkeypatch.setattr(test_runner, "runner_version",
                        lambda: {"commit": "a" * 40, "dirty": False,
                                 "timeouts": {"xcodebuild_test": 900}})
    with caplog.at_level(logging.WARNING):
        test_runner._log_runner_version("xcodebuild_test", 1200)
    assert "is inert" in caplog.text
    assert "900" in caplog.text and "1200" in caplog.text


def test_agreeing_tables_warn_about_nothing(monkeypatch, caplog):
    monkeypatch.setattr(test_runner, "runner_version",
                        lambda: {"commit": "b" * 40, "dirty": False,
                                 "timeouts": dict(test_runner.DEFAULT_TIMEOUTS)})
    with caplog.at_level(logging.WARNING):
        test_runner._log_runner_version("xcodebuild_test", 1200)
    assert "inert" not in caplog.text


def test_an_old_runner_says_so_and_never_blocks_a_dispatch(monkeypatch, caplog):
    """A 404 means the runner predates this endpoint, which is itself the
    answer to 'is the fix live?'."""
    class _Resp:
        status_code = 404
        def json(self): return {}

    monkeypatch.setattr(test_runner._SESSION, "get", lambda *a, **k: _Resp())
    assert test_runner.runner_version() is None
    with caplog.at_level(logging.INFO):
        test_runner._log_runner_version("xcodebuild_test", 1200)
    assert "predates DEV-805" in caplog.text


def test_an_unreachable_runner_is_telemetry_not_an_error(monkeypatch):
    monkeypatch.setattr(test_runner._SESSION, "get", lambda *a, **k: (_ for _ in ()).throw(
        requests.RequestException("down")))
    assert test_runner.runner_version() is None
    test_runner._log_runner_version("xcodebuild_test", 1200)   # must not raise
