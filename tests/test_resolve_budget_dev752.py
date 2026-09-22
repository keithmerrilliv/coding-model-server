"""DEV-752: the resolve pre-step gets its own bounded budget, a resolve that
times out fails the dispatch instead of starting a doomed test, and the warm
SwiftPM cache's state reaches the artifact.

Run 44 spent the whole 1200s budget on a cold MLX resolve and reached a
code-review gate having executed no test at all. The gate was honest about it,
but "inconclusive" beside 16 KB of plausible build output is a failure that
reads like a result.
"""
import subprocess
import types

import pytest
from fastapi.testclient import TestClient

from mac_runner import server, vm
from mac_runner.config import Config


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    return path


@pytest.fixture
def client(tmp_path, repo, monkeypatch):
    repos_file = tmp_path / "repos.yml"
    repos_file.write_text(f"repos:\n  proj:\n    path: {repo}\n")
    monkeypatch.setattr(Config, "REPOS_FILE", repos_file)
    monkeypatch.setattr(Config, "API_KEY", "test-key")
    monkeypatch.setattr(Config, "WORKTREE_ROOT", tmp_path / "wt")
    monkeypatch.setattr(Config, "DERIVED_DATA", tmp_path / "dd")
    monkeypatch.setattr(Config, "SANDBOX", False)
    monkeypatch.setattr(Config, "VM", False)
    return TestClient(server.app)


# ── the budget itself ────────────────────────────────────────────────────────

def test_resolve_never_starves_the_test_phase(monkeypatch):
    monkeypatch.setattr(server, "RESOLVE_TIMEOUT", 900)
    monkeypatch.setattr(server, "MIN_TEST_BUDGET", 600)
    # Generous budget: resolve takes its full ceiling and the test keeps the rest.
    assert server.resolve_budget(2400) == 900
    # Tight budget: the floor wins, so the test still gets MIN_TEST_BUDGET.
    assert server.resolve_budget(900) == 300
    # Absurd budget: a 60s floor, never zero, never negative.
    assert server.resolve_budget(300) == 60
    for total in (300, 900, 1200, 2400):
        assert server.resolve_budget(total) >= 60
        assert total - server.resolve_budget(total) >= min(600, total - 60)


def test_the_dispatch_hands_the_vm_a_capped_budget_and_a_floor(client, monkeypatch):
    """The values the VM path is given come from resolve_budget, not the raw timeout."""
    monkeypatch.setattr(Config, "SANDBOX", True)
    monkeypatch.setattr(Config, "VM", True)
    monkeypatch.setattr(server, "_sandbox_available", lambda: True)
    monkeypatch.setattr(server.vm, "vm_available", lambda: None)
    seen = {}

    def fake_vm_run(wt, resolve_cmd, cmd, *, timeout, resolve_timeout,
                    min_test_budget=0, warnings=None):
        seen.update(timeout=timeout, resolve_timeout=resolve_timeout,
                    min_test_budget=min_test_budget)
        return 0, "guest tests ok"

    monkeypatch.setattr(server.vm, "run_tests_in_vm", fake_vm_run)
    resp = client.post(
        "/v1/run_tests",
        headers={"X-Runner-Key": "test-key"},
        json={"spec_id": "s1", "repo": "proj", "framework": "xcodebuild_test",
              "scheme": "Demo"},
    )
    assert resp.status_code == 200
    assert seen["timeout"] == 2400                      # the DEV-752 ceiling
    assert seen["resolve_timeout"] == server.resolve_budget(2400)
    assert seen["min_test_budget"] == min(server.MIN_TEST_BUDGET, 2400)
    assert seen["resolve_timeout"] + seen["min_test_budget"] <= seen["timeout"]


# ── a resolve timeout is fatal, on both execution paths ──────────────────────

def test_host_path_refuses_to_start_a_test_after_a_resolve_timeout(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-resolvePackageDependencies" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
        pytest.fail(f"no test may start after a resolve timeout, got {cmd}")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    req = types.SimpleNamespace(framework="xcodebuild_test")
    passed, output, exit_code = server._run_on_host(
        req, tmp_path, ["xcodebuild", "test"],
        ["xcodebuild", "-resolvePackageDependencies"], 2400)

    assert passed is False
    assert exit_code is None, "None marks infrastructure, not a verdict"
    assert "package resolution timed out" in output
    assert "no test was run" in output
    assert len(calls) == 1, "the test command must never have been spawned"


def test_host_path_still_tolerates_a_resolve_that_merely_fails(monkeypatch, tmp_path):
    """A non-zero resolve stays non-fatal (DEV-294): a warm cache may carry it."""
    started = []

    def fake_run(cmd, **kw):
        started.append(cmd)
        if "-resolvePackageDependencies" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "could not resolve Gzip")
        return subprocess.CompletedProcess(cmd, 0, "** TEST SUCCEEDED **", "")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(Config, "SANDBOX", False)
    req = types.SimpleNamespace(framework="xcodebuild_test")
    passed, output, exit_code = server._run_on_host(
        req, tmp_path, ["xcodebuild", "test"],
        ["xcodebuild", "-resolvePackageDependencies"], 2400)

    assert passed is True and exit_code == 0
    assert "package resolution failed" in output
    assert len(started) == 2, "the test still runs"


# ── the warm cache's state reaches the artifact ──────────────────────────────

def test_cache_summary_names_what_it_covers(tmp_path):
    cache = tmp_path / "pkgcache"
    (cache / "checkouts").mkdir(parents=True)
    for name in ("mlx-swift", "swift-transformers", "Jinja"):
        (cache / "checkouts" / name).mkdir()
    summary = vm._cache_summary(cache)
    assert "3 package(s)" in summary
    assert "mlx-swift" in summary and "Jinja" in summary


def test_cache_summary_reports_an_empty_or_unreadable_cache(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert vm._cache_summary(empty) == "empty"
    assert "unreadable" in vm._cache_summary(tmp_path / "does-not-exist")


def test_an_unconfigured_cache_is_said_so_in_the_output(monkeypatch):
    """The diagnosis run 44 needed, in the artifact rather than the Mac's log."""
    monkeypatch.setattr(Config, "VM_PACKAGE_CACHE", "")
    assert vm._package_cache_dir() is None
