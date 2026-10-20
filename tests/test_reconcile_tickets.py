"""DEV-804 — scripts/reconcile_tickets.py reports the gap between Jira and git.

The buckets are the three drifts that hid before the check existed (DEV-514,
DEV-537): shipped-but-open, closed-but-unshipped, and the one only a host can
see about itself, closed-but-unpushed. The git side runs against a real temp
repository with a bare origin; the Jira side is a fake source.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reconcile_tickets.py"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def rt():
    spec = importlib.util.spec_from_file_location("reconcile_tickets", _SRC)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolve the script's postponed
    # annotations through sys.modules[cls.__module__].
    sys.modules["reconcile_tickets"] = module
    spec.loader.exec_module(module)
    return module


def _git(repo, *args, when="2026-10-01T12:00:00"):
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], check=True, capture_output=True, text=True, env=env).stdout


def _commit(repo, name, subject, when="2026-10-01T12:00:00"):
    (repo / name).write_text(subject)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject, when=when)


@pytest.fixture
def repo(tmp_path):
    """main with DEV-1 and DEV-7 pushed; DEV-2 on a local-only branch; DEV-8 on
    main but not yet pushed."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "a", "DEV-1: old fix, ticket never closed", when="2026-09-20T12:00:00")
    _commit(work, "b", "DEV-7: landed yesterday, still in review", when="2026-10-18T12:00:00")
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(work, "checkout", "-q", "-b", "DEV-2-local")
    _commit(work, "c", "DEV-2: done in Jira, commit only here")
    _git(work, "checkout", "-q", "main")
    _commit(work, "d", "DEV-8: on main, not pushed")
    return work


class FakeSource:
    def __init__(self, rt, tickets):
        self.T = rt.Ticket
        self.tickets = {t.key: t for t in tickets}
        self.fetched = []

    def fetch(self, keys):
        self.fetched.append(list(keys))
        return {k: self.tickets[k] for k in keys if k in self.tickets}

    def closed_since(self, project, since):
        return [t for t in self.tickets.values()
                if t.key.startswith(project + "-") and t.category == "done"]


def _tickets(rt):
    T = rt.Ticket
    return [
        T("DEV-1", "In Review", "indeterminate", "Task"),
        T("DEV-2", "Done", "done", "Task", resolution="Done", summary="only local"),
        T("DEV-3", "Done", "done", "Bug", resolution="Done", resolved_at="2026-10-10",
          summary="no commit anywhere"),
        T("DEV-4", "Done", "done", "Epic", resolution="Done", summary="epics ship nothing"),
        T("DEV-5", "Done", "done", "Task", resolution="Won't Do", summary="not actioned"),
        T("DEV-6", "Done", "done", "Task", resolution="Done", labels=["hardware"],
          summary="a DIMM swap"),
        T("DEV-7", "In Review", "indeterminate", "Task"),
        T("DEV-8", "Done", "done", "Task", resolution="Done", summary="main not pushed"),
    ]


def _run(rt, repo, source, **kw):
    facts = rt.collect_repo(repo)
    opts = dict(project="DEV", now=NOW, min_age_days=2, since_days=90, ignore_labels=["hardware"])
    opts.update(kw)
    return rt.reconcile([facts], source, **opts)


class TestGitFacts:
    def test_shipped_means_origin_main_not_local_main(self, rt, repo):
        facts = rt.collect_repo(repo)
        assert facts.shipped_ref == "origin/main"
        assert set(facts.shipped) == {"DEV-1", "DEV-7"}
        assert set(facts.unpushed) == {"DEV-2", "DEV-8"}
        assert facts.branches_ahead == {"DEV-2-local": 1, "main": 1}
        assert facts.main_ahead == 1
        assert facts.unpushed_work

    def test_repo_without_remote_falls_back_to_main(self, rt, tmp_path):
        solo = tmp_path / "solo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(solo)], check=True)
        _commit(solo, "a", "DEV-9: nowhere to push")
        facts = rt.collect_repo(solo)
        assert facts.shipped_ref == "main"
        assert set(facts.shipped) == {"DEV-9"}
        assert facts.main_ahead == 0


class TestBuckets:
    def test_each_drift_lands_in_its_bucket(self, rt, repo):
        source = FakeSource(rt, _tickets(rt))
        report = _run(rt, repo, source)
        buckets = {f.key: f.bucket for f in report.findings}
        assert buckets == {
            "DEV-1": "shipped-but-open",
            "DEV-2": "closed-but-unpushed",
            "DEV-3": "closed-but-unshipped",
            "DEV-8": "closed-but-unpushed",
        }
        assert report.drift

    def test_only_shipped_keys_are_looked_up(self, rt, repo):
        """Jira is asked about what git shipped, not about every key ever."""
        source = FakeSource(rt, _tickets(rt))
        _run(rt, repo, source)
        assert source.fetched == [["DEV-1", "DEV-7"]]

    def test_fresh_commits_are_not_drift(self, rt, repo):
        """DEV-7 landed yesterday and is In Review: that is the normal path."""
        source = FakeSource(rt, _tickets(rt))
        keys = {f.key for f in _run(rt, repo, source).findings}
        assert "DEV-7" not in keys
        keys = {f.key for f in _run(rt, repo, source, min_age_days=0).findings}
        assert "DEV-7" in keys

    def test_epics_wont_do_and_ignored_labels_are_skipped(self, rt, repo):
        source = FakeSource(rt, _tickets(rt))
        keys = {f.key for f in _run(rt, repo, source).findings}
        assert not keys & {"DEV-4", "DEV-5", "DEV-6"}
        report = _run(rt, repo, source, ignore_labels=[])
        dev6 = next(f for f in report.findings if f.key == "DEV-6")
        assert dev6.labels == ["hardware"], "labels ride along so a bulk closure is recognisable"
        assert "[hardware]" in rt.render_text(report)

    def test_unpushed_detail_names_where_the_commit_lives(self, rt, repo):
        source = FakeSource(rt, _tickets(rt))
        report = _run(rt, repo, source)
        dev2 = next(f for f in report.findings if f.key == "DEV-2")
        assert "local-only" in dev2.detail
        assert dev2.commits and dev2.commits[0].subject.startswith("DEV-2")


class TestOutput:
    def test_text_report_reads_top_down(self, rt, repo):
        report = _run(rt, repo, FakeSource(rt, _tickets(rt)))
        text = rt.render_text(report)
        assert "Shipped but ticket not Done: 1" in text
        assert "Ticket Done but the commit is only on this host: 2" in text
        assert "Ticket Done but no commit mentions it: 1" in text
        assert "main ahead of origin/main by 1" in text and "DEV-2-local +1" in text

    def test_json_is_the_same_report(self, rt, repo):
        report = _run(rt, repo, FakeSource(rt, _tickets(rt)))
        data = json.loads(rt.render_json(report))
        assert {f["key"] for f in data["findings"]} == {"DEV-1", "DEV-2", "DEV-3", "DEV-8"}
        assert data["repos"][0]["main_ahead"] == 1

    def test_clean_repo_and_board_report_no_drift(self, rt, tmp_path):
        origin = tmp_path / "o.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        work = tmp_path / "w"
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        _git(work, "remote", "add", "origin", str(origin))
        _commit(work, "a", "DEV-1: shipped and closed")
        _git(work, "push", "-q", "-u", "origin", "main")
        source = FakeSource(rt, [rt.Ticket("DEV-1", "Done", "done", "Task", resolution="Done")])
        report = _run(rt, work, source)
        assert not report.findings and not report.drift
        assert "clean" in rt.render_text(report)


class TestEnvFile:
    def test_env_file_is_read_not_exported(self, rt, tmp_path, monkeypatch):
        """DEV-508: a script that exports .env poisons every later import."""
        env = tmp_path / ".env"
        env.write_text('JIRA_URL="https://x.atlassian.net/"\n# c\nJIRA_EMAIL=a@b\n\nBAD LINE\n')
        monkeypatch.delenv("JIRA_URL", raising=False)
        values = rt.read_env_file(env)
        assert values == {"JIRA_URL": "https://x.atlassian.net/", "JIRA_EMAIL": "a@b"}
        assert "JIRA_URL" not in os.environ

    def test_missing_credential_is_a_clear_exit(self, rt, tmp_path, monkeypatch):
        for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(SystemExit, match="JIRA_URL is not set"):
            rt.jira_from_env(tmp_path / "absent.env")
