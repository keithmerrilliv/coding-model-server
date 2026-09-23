"""A failed `read_files` has to leave a line in the orchestrator's own log.

`fetch_repo_files` fails soft by design (DEV-620): a non-200 becomes a single
unprefixed `problems` entry and the execution pass carries on with less context
than it asked for. It also returned early WITHOUT logging, so when 747 requests
were rejected 401 over nine days the orchestrator's journal held zero lines for
any of them — the count was recoverable only from the runner's uvicorn access
log on the other host.

Failing soft is right. Failing silently is not. These pin the log line, and pin
that adding it did not change the fail-soft shape `problems_indicate_runner_outage`
depends on.

Lives apart from the server-side half in test_runner_auth_rejection_visible.py
because this half needs the orchestrator's dependencies, which the Mac's own
runner venv does not carry.
"""
import logging

import pytest

from coding_model_autonomous import test_runner


@pytest.fixture
def remote_repo(monkeypatch):
    """Force the remote path: a self-target read never touches the runner."""
    monkeypatch.setattr(test_runner, "_is_self_target_repo", lambda repo: False)


def test_a_401_is_logged_with_both_causes_to_check(remote_repo, monkeypatch, caplog):
    class Resp:
        status_code = 401
        text = "invalid or missing runner key"

    monkeypatch.setattr(test_runner, "MAC_RUNNER_API_KEY", "some-key")
    monkeypatch.setattr(test_runner._SESSION, "post", lambda *a, **k: Resp())
    with caplog.at_level(logging.WARNING, logger="orchestrator.test_runner"):
        files, problems = test_runner.fetch_repo_files(
            "electric-sheep", ["a.swift", "b.swift"], "origin/main")
    assert files == []
    assert problems == ["read_files HTTP 401: invalid or missing runner key"]
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "401" in msg
    assert "MAC_RUNNER_API_KEY" in msg, "name the key to compare"
    assert "second mac_runner.server" in msg, "name the other cause"
    assert test_runner.problems_indicate_runner_outage(problems, ["a.swift", "b.swift"])


def test_a_404_and_a_transport_error_are_logged_too(remote_repo, monkeypatch, caplog):
    import requests

    class Gone:
        status_code = 404
        text = "Not Found"

    monkeypatch.setattr(test_runner, "MAC_RUNNER_API_KEY", "some-key")
    monkeypatch.setattr(test_runner._SESSION, "post", lambda *a, **k: Gone())
    with caplog.at_level(logging.WARNING, logger="orchestrator.test_runner"):
        _, problems = test_runner.fetch_repo_files(
            "electric-sheep", ["a.swift"], "origin/main")
    assert problems == ["runner has no /v1/read_files route (needs redeploy)"]
    assert "FAILED" in " ".join(r.getMessage() for r in caplog.records)

    caplog.clear()

    def boom(*a, **k):
        raise requests.RequestException("connection reset")

    monkeypatch.setattr(test_runner._SESSION, "post", boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator.test_runner"):
        _, problems = test_runner.fetch_repo_files(
            "electric-sheep", ["a.swift"], "origin/main")
    assert "connection reset" in problems[0]
    assert "FAILED" in " ".join(r.getMessage() for r in caplog.records)
