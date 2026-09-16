"""DEV-701: a read must say which commit served it, and whether that clone is current.

Run 39 built Centipede slice 8 on a Mac clone that predated slice 7 entirely.
Every stage behaved correctly given its input — the architect designed against
26 tests, the implementer wrote against a Game.swift with no slice 7, the
pre-gate suite ran on that same tree and PASSED, the release gate approved, and
delivery pushed the branch. The damage appeared only at the comparison
boundary: against origin the branch read as deleting six tests and 37 lines.

Nothing was deleted. `base_ref: main` had resolved to a main, just not the one
anyone meant, because nothing anywhere compared the local clone to its remote.
That is the most expensive shape a defect can take — internally consistent,
externally wrong, surviving every check until after the compute is spent.

These pin the two cheap halves of the fix: report the sha, and detect the drift.
"""
import subprocess
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from mac_runner import server
from mac_runner.config import Config


def _git(path, *args, **kw):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=False, **kw)


def _commit(path, name, body):
    (path / name).write_text(body)
    _git(path, "add", "-A")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A bare remote plus a clone of it — a real one, because this is all git."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "a.txt", "one\n")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return origin, seed, clone


# ── the runner's own report ──────────────────────────────────────────────────

def test_a_current_clone_reports_in_sync(upstream):
    _, _, clone = upstream
    st = server._ref_state(clone, "main")
    assert st.in_sync is True
    assert st.local_sha == st.remote_sha
    assert st.note is None


def test_a_stale_clone_is_detected_and_says_so(upstream):
    origin, seed, clone = upstream
    # Origin moves on; the clone is never told.
    new_sha = _commit(seed, "b.txt", "two\n")
    _git(seed, "push", "-q", str(origin), "main")

    st = server._ref_state(clone, "main")
    assert st.in_sync is False, "a clone behind its remote must not read as clear"
    assert st.remote_sha == new_sha
    assert st.local_sha != new_sha
    assert "NOT" in (st.note or "")


def test_staleness_is_read_from_the_remote_not_the_tracking_ref(upstream):
    # The whole point. refs/remotes/origin/main is only as fresh as the last
    # fetch, so a stale clone's tracking ref agrees with its stale local ref
    # and would report all clear. ls-remote is the only truth without fetching.
    origin, seed, clone = upstream
    _commit(seed, "b.txt", "two\n")
    _git(seed, "push", "-q", str(origin), "main")

    tracking = _git(clone, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    local = _git(clone, "rev-parse", "main").stdout.strip()
    assert tracking == local, "precondition: the stale clone's tracking ref agrees"

    st = server._ref_state(clone, "main")
    assert st.in_sync is False


def test_an_unreachable_remote_reports_unknown_not_clear(upstream, monkeypatch):
    _, _, clone = upstream
    monkeypatch.setattr(server, "_remote_sha", lambda *a, **k: None)
    st = server._ref_state(clone, "main")
    assert st.in_sync is None, "unknown must never be reported as in sync"
    assert "UNKNOWN" in (st.note or "")
    assert st.local_sha  # the local half is still reported


def test_a_ref_that_does_not_resolve_is_named(upstream):
    _, _, clone = upstream
    st = server._ref_state(clone, "no-such-branch")
    assert st.local_sha is None
    assert "does not resolve" in (st.note or "")


def test_remote_lookup_is_cached_so_chunked_reads_pay_once(upstream):
    _, _, clone = upstream
    server._REMOTE_CACHE.clear()
    calls = []
    real = subprocess.run

    def counting(cmd, *a, **k):
        if "ls-remote" in cmd:
            calls.append(cmd)
        return real(cmd, *a, **k)

    with mock.patch.object(server.subprocess, "run", counting):
        for _ in range(4):
            server._ref_state(clone, "main")
    assert len(calls) == 1, f"ls-remote ran {len(calls)} times; context assembly chunks reads"


@pytest.mark.parametrize("boom", [
    OSError("git binary missing"),
    subprocess.TimeoutExpired(cmd="git", timeout=15),
])
def test_git_failing_outright_degrades_to_unknown_rather_than_raising(upstream, boom):
    # Telemetry must never cost a read. This is a dispatch-path endpoint, and
    # a missing git or a hung network call has to surface as "I do not know",
    # not as an exception that denies the implementer its files.
    _, _, clone = upstream
    with mock.patch.object(server.subprocess, "run", side_effect=boom):
        st = server._ref_state(clone, "main")          # must not raise
    assert st.local_sha is None
    assert st.in_sync is not True


# ── the endpoint carries it ──────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, upstream, monkeypatch):
    _, _, clone = upstream
    repos_file = tmp_path / "repos.yml"
    repos_file.write_text(f"repos:\n  proj:\n    path: {clone}\n")
    monkeypatch.setattr(Config, "REPOS_FILE", repos_file)
    monkeypatch.setattr(Config, "API_KEY", "test-key")
    monkeypatch.setattr(Config, "SANDBOX", False)
    monkeypatch.setattr(Config, "VM", False)
    server._REMOTE_CACHE.clear()
    return TestClient(server.app)


def test_read_files_reports_the_commit_it_served(client):
    r = client.post("/v1/read_files",
                    json={"repo": "proj", "base_ref": "main", "paths": ["a.txt"]},
                    headers={"X-Runner-Key": "test-key"})
    assert r.status_code == 200
    state = r.json()["ref_state"]
    assert state["ref"] == "main"
    assert len(state["local_sha"]) == 40
    assert state["in_sync"] is True


def test_read_files_flags_a_stale_clone(client, upstream):
    origin, seed, _ = upstream
    _commit(seed, "b.txt", "two\n")
    _git(seed, "push", "-q", str(origin), "main")
    server._REMOTE_CACHE.clear()

    r = client.post("/v1/read_files",
                    json={"repo": "proj", "base_ref": "main", "paths": ["a.txt"]},
                    headers={"X-Runner-Key": "test-key"})
    body = r.json()
    assert body["ref_state"]["in_sync"] is False
    # and the content is still served — a stale answer beats no answer, as
    # long as it is labelled
    assert body["files"][0]["content"] == "one\n"


# ── the orchestrator receives it ─────────────────────────────────────────────

class _Resp:
    status_code = 200
    def __init__(self, body): self._b = body
    def json(self): return self._b


def _patched_session(monkeypatch, body):
    from coding_model_autonomous import test_runner as tr
    monkeypatch.setattr(tr, "_SESSION",
                        mock.Mock(post=mock.Mock(return_value=_Resp(body))))
    monkeypatch.setattr(tr, "_is_self_target_repo", lambda repo: False)
    return tr


def test_fetch_repo_files_fills_the_caller_s_ref_state(monkeypatch):
    tr = _patched_session(monkeypatch, {
        "files": [{"path": "a.txt", "content": "x"}],
        "ref_state": {"ref": "main", "local_sha": "a" * 40,
                      "remote_sha": "a" * 40, "in_sync": True},
    })
    state: dict = {}
    files, problems = tr.fetch_repo_files("proj", ["a.txt"], "main", ref_state=state)
    assert files == [("a.txt", "x")]
    assert state["in_sync"] is True
    assert state["source"] == "runner"


def test_an_older_runner_leaves_staleness_unknown_not_clear(monkeypatch):
    # The runner and the server deploy independently and in either order.
    # A runner without the field must not read as "in sync".
    tr = _patched_session(monkeypatch, {"files": [{"path": "a.txt", "content": "x"}]})
    state: dict = {}
    files, _ = tr.fetch_repo_files("proj", ["a.txt"], "main", ref_state=state)
    assert files == [("a.txt", "x")]
    assert "in_sync" not in state


def test_a_stale_clone_is_not_reported_as_a_runner_outage(monkeypatch):
    # The trap this avoids: folding staleness into `problems` would add an
    # unprefixed entry, and problems_indicate_runner_outage reads exactly that
    # shape as a dead runner (DEV-620) — parking a spec over a healthy read.
    tr = _patched_session(monkeypatch, {
        "files": [{"path": "a.txt", "content": "x"}],
        "ref_state": {"ref": "main", "local_sha": "a" * 40,
                      "remote_sha": "b" * 40, "in_sync": False,
                      "note": "this clone's main is NOT origin/main"},
    })
    state: dict = {}
    files, problems = tr.fetch_repo_files("proj", ["a.txt"], "main", ref_state=state)
    assert state["in_sync"] is False
    assert problems == []
    assert not tr.problems_indicate_runner_outage(problems, ["a.txt"])


def test_a_stale_clone_still_returns_its_files(monkeypatch):
    # Refusing here would be a bigger outage than the staleness. The decision
    # to refuse belongs upstream, with the spec in hand.
    tr = _patched_session(monkeypatch, {
        "files": [{"path": "a.txt", "content": "stale but real"}],
        "ref_state": {"ref": "main", "in_sync": False, "local_sha": "a" * 40},
    })
    files, _ = tr.fetch_repo_files("proj", ["a.txt"], "main", ref_state={})
    assert files == [("a.txt", "stale but real")]


def test_the_warning_names_the_repo_and_the_drift(monkeypatch, caplog):
    tr = _patched_session(monkeypatch, {
        "files": [{"path": "a.txt", "content": "x"}],
        "ref_state": {"ref": "main", "in_sync": False,
                      "note": "serving deadbeef while the remote is at cafebabe"},
    })
    with caplog.at_level("WARNING"):
        tr.fetch_repo_files("centipede", ["a.txt"], "main", ref_state={})
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "STALE CLONE" in joined
    assert "centipede" in joined
    assert "cafebabe" in joined


# ── and it lands on the event ────────────────────────────────────────────────

def test_ref_state_rides_onto_the_context_assembled_payload():
    from coding_model_autonomous.context import SpecContext
    ctx = SpecContext(spec_id="s", repo="centipede", base_ref="main",
                      candidates=[], declared=[], protected_paths=[],
                      ref_state={"local_sha": "c2748d18", "in_sync": False})
    summary = ctx.summary()
    assert summary["ref_state"]["local_sha"] == "c2748d18"
    assert summary["ref_state"]["in_sync"] is False


def test_an_absent_ref_state_does_not_add_an_empty_key():
    # An older runner should leave the payload shaped as it was, not add a
    # misleading empty dict that reads as "we checked and found nothing".
    from coding_model_autonomous.context import SpecContext
    ctx = SpecContext(spec_id="s", repo="centipede", base_ref="main",
                      candidates=[], declared=[], protected_paths=[])
    assert "ref_state" not in ctx.summary()
