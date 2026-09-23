"""An auth rejection has to name itself, on both sides of the link.

747 `POST /v1/read_files` requests were rejected 401 over nine days and left no
trace anywhere but the runner's uvicorn access log:

  * the server logged nothing at all on a rejection, so there was no caller
    address, no timestamp in the application log, and no way to tell a client
    that never loaded its key from one presenting the wrong key;
  * `fetch_repo_files` returned the non-200 as a soft `problems` entry and
    returned early WITHOUT logging (DEV-620 makes it fail soft on purpose), so
    the orchestrator's journal held zero lines for all 747.

One contributor is known: zooshly carries a `coding-model-runner-shim` unit
that runs this same server locally, for serving reads while the Mac's tunnel is
down. It binds the port the tunnel publishes there and reads a Linux clone, and
its own journal shows it rejecting reads on 13 Sep. It is a deliberate stopgap;
what made it dangerous is that nothing distinguished it from the Mac. So the
server now refuses to start off Darwin unless that is declared.

Whether the shim accounts for ALL of the rejections is not established, and
cannot be: neither side recorded enough to attribute them. That is the point.
These pin the visibility rather than the cause, so the next occurrence is
legible from the logs alone.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from mac_runner import server
from mac_runner.config import Config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "the-real-key")
    monkeypatch.setattr(Config, "ALLOW_UNAUTH", False)
    return TestClient(server.app)


# ── server side ──────────────────────────────────────────────────────────────

def test_rejection_logs_method_path_and_caller(client, caplog):
    with caplog.at_level(logging.WARNING, logger="mac_runner.server"):
        resp = client.get("/v1/repos", headers={"X-Runner-Key": "wrong"})
    assert resp.status_code == 401
    rec = [r for r in caplog.records if "AUTH REJECTED" in r.getMessage()]
    assert len(rec) == 1, "a rejection must log exactly once"
    msg = rec[0].getMessage()
    assert "GET" in msg and "/v1/repos" in msg
    assert "testclient" in msg or "?" in msg


def test_rejection_never_logs_the_key_or_a_prefix_of_it(client, caplog):
    with caplog.at_level(logging.WARNING, logger="mac_runner.server"):
        client.get("/v1/repos", headers={"X-Runner-Key": "wrong-but-secret"})
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "the-real-key" not in logged
    assert "wrong-but-secret" not in logged
    # not even a prefix — a prefix leaks a little with every rejection
    assert "wrong-b" not in logged
    assert "the-rea" not in logged


def test_absent_key_is_distinguishable_from_a_wrong_one(client, caplog):
    with caplog.at_level(logging.WARNING, logger="mac_runner.server"):
        client.get("/v1/repos")
    absent = " ".join(r.getMessage() for r in caplog.records)
    assert "presented absent" in absent, "a caller that sent no header must say so"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mac_runner.server"):
        client.get("/v1/repos", headers={"X-Runner-Key": "wrong"})
    wrong = " ".join(r.getMessage() for r in caplog.records)
    assert "presented absent" not in wrong
    assert server._key_fingerprint("wrong") in wrong
    assert server._key_fingerprint("the-real-key") in wrong, (
        "the expected fingerprint is what tells a rotation from a stranger")


def test_fingerprint_is_not_reversible_and_flags_empty():
    assert server._key_fingerprint("") == "absent"
    fp = server._key_fingerprint("abcdef123456")
    assert len(fp) == 8
    assert "abcdef" not in fp


def test_a_good_key_logs_no_rejection(client, caplog):
    with caplog.at_level(logging.WARNING, logger="mac_runner.server"):
        resp = client.get("/v1/repos", headers={"X-Runner-Key": "the-real-key"})
    assert resp.status_code == 200
    assert not [r for r in caplog.records if "AUTH REJECTED" in r.getMessage()]


# ── the runner refuses to be the Mac on a host that is not one ───────────────

def test_main_refuses_to_start_off_darwin(monkeypatch, caplog):
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(Config, "ALLOW_NON_DARWIN", False)
    with caplog.at_level(logging.ERROR, logger="mac_runner.server"):
        with pytest.raises(SystemExit) as exc:
            server.main()
    assert exc.value.code == 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "refuses to start on linux" in msg


def test_the_override_opens_the_platform_gate(monkeypatch, caplog):
    """With the override set, main() must get PAST the platform check.

    Proven by the next guard firing instead: an empty API_KEY. That keeps this
    test from having to start a real server to show the gate opened.
    """
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(Config, "ALLOW_NON_DARWIN", True)
    monkeypatch.setattr(Config, "API_KEY", "")
    monkeypatch.setattr(Config, "ALLOW_UNAUTH", False)
    with caplog.at_level(logging.ERROR, logger="mac_runner.server"):
        with pytest.raises(SystemExit) as exc:
            server.main()
    assert exc.value.code == 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "refuses to start on linux" not in msg
    assert "CODING_MODEL_RUNNER_API_KEY is not set" in msg


def test_darwin_does_not_trip_the_platform_guard(monkeypatch, caplog):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(Config, "ALLOW_NON_DARWIN", False)
    monkeypatch.setattr(Config, "API_KEY", "")
    monkeypatch.setattr(Config, "ALLOW_UNAUTH", False)
    with caplog.at_level(logging.ERROR, logger="mac_runner.server"):
        with pytest.raises(SystemExit):
            server.main()
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "refuses to start" not in msg
