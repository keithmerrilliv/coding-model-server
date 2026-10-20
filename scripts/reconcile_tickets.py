#!/usr/bin/env python3
"""Reconcile ticket status against code state, and show work this host has not pushed.

Two drifts hid in the open for weeks before this existed (DEV-804, consolidating
DEV-514 and DEV-537):

  - a ticket whose fix is on main but which still reads To Do / In Review, and a
    ticket marked Done whose key appears in no commit anywhere;
  - a commit that exists on one host only. It survives a branch listing, a
    `git log main` and a source grep on every other machine, because none of
    them can see it.

Both are facts about the gap between two systems of record, so the check reads
both and reports the difference, nothing else. It changes neither.

    scripts/reconcile_tickets.py                 # this repo, project DEV, text report
    scripts/reconcile_tickets.py --repo ~/Dev/llab --repo .   # several repos
    scripts/reconcile_tickets.py --json          # machine-readable
    scripts/reconcile_tickets.py --strict        # exit 1 when anything drifts

Buckets:

  shipped-but-open      key is on the shipped ref (origin/main) but the ticket is
                        not Done. Commits younger than --min-age-days are skipped
                        so in-flight work is not noise.
  closed-but-unpushed   ticket is Done and the key appears only on a local branch
                        or in commits main has not pushed. The DEV-537 case.
  closed-but-unshipped  ticket is Done (resolution Done, not an Epic) and the key
                        appears in no commit the host can see. Tickets with a
                        --ignore-label label are skipped; hardware and process
                        tickets legitimately ship no code.
  unpushed-work         branches ahead of every remote, main ahead of origin,
                        stashes, a dirty tree. Per repo.

Jira credentials come from JIRA_URL / JIRA_EMAIL / JIRA_API_TOKEN, read from the
environment or from --env-file (default: the repo's .env). The file is parsed
here and not exported: a script that exports .env into os.environ poisons every
import after it (DEV-508).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Sequence

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

DONE = "done"  # Jira statusCategory keys: new | indeterminate | done


# ── Jira side ────────────────────────────────────────────────────────────────

@dataclass
class Ticket:
    key: str
    status: str
    category: str                      # new | indeterminate | done
    issue_type: str = ""
    resolution: Optional[str] = None   # "Done", "Won't Do", "Duplicate", None
    labels: List[str] = field(default_factory=list)
    resolved_at: Optional[str] = None  # ISO date
    summary: str = ""


class TicketSource(Protocol):
    def fetch(self, keys: Sequence[str]) -> Dict[str, Ticket]: ...
    def closed_since(self, project: str, since: datetime) -> List[Ticket]: ...


def _ticket_from_api(raw: dict) -> Ticket:
    f = raw.get("fields", {}) or {}
    status = f.get("status") or {}
    resolution = f.get("resolution") or {}
    return Ticket(
        key=raw["key"],
        status=status.get("name", ""),
        category=((status.get("statusCategory") or {}).get("key") or "").lower(),
        issue_type=(f.get("issuetype") or {}).get("name", ""),
        resolution=resolution.get("name") if isinstance(resolution, dict) else None,
        labels=list(f.get("labels") or []),
        resolved_at=(f.get("resolutiondate") or "")[:10] or None,
        summary=f.get("summary", "") or "",
    )


class AtlassianSource:
    """Jira Cloud through atlassian-python-api, the library the daemon uses."""

    FIELDS = "summary,status,resolution,issuetype,labels,resolutiondate"
    PAGE = 100

    def __init__(self, url: str, email: str, token: str) -> None:
        from atlassian import Jira  # lazy: the tests never need it
        self._jira = Jira(url=url.strip().rstrip("/"), username=email.strip(),
                          password=token.strip(), cloud=True)

    def _search(self, jql: str) -> List[Ticket]:
        out: List[Ticket] = []
        start = 0
        while True:
            resp = self._jira.jql(jql, fields=self.FIELDS, start=start, limit=self.PAGE)
            if not isinstance(resp, dict) or "issues" not in resp:
                msg = resp.get("errorMessages", ["unknown error"])[0] \
                    if isinstance(resp, dict) else str(resp)
                raise RuntimeError(f"Jira JQL failed (auth or permissions?): {msg}")
            page = resp["issues"]
            out.extend(_ticket_from_api(r) for r in page)
            start += len(page)
            if not page or start >= int(resp.get("total", start)):
                return out

    def fetch(self, keys: Sequence[str]) -> Dict[str, Ticket]:
        found: Dict[str, Ticket] = {}
        keys = sorted(set(keys))
        for i in range(0, len(keys), self.PAGE):
            chunk = ", ".join(keys[i:i + self.PAGE])
            for t in self._search(f"key in ({chunk})"):
                found[t.key] = t
        return found

    def closed_since(self, project: str, since: datetime) -> List[Ticket]:
        day = since.strftime("%Y-%m-%d")
        return self._search(
            f'project = "{project}" AND statusCategory = Done AND resolved >= "{day}"'
            " ORDER BY key ASC")


# ── Git side ─────────────────────────────────────────────────────────────────

@dataclass
class Commit:
    sha: str
    date: str      # ISO date
    subject: str
    where: str     # ref or branch the commit was seen on


@dataclass
class RepoFacts:
    path: str
    shipped_ref: str
    shipped: Dict[str, List[Commit]] = field(default_factory=dict)   # key -> commits on shipped_ref
    unpushed: Dict[str, List[Commit]] = field(default_factory=dict)  # key -> commits not on any remote
    branches_ahead: Dict[str, int] = field(default_factory=dict)     # branch -> commits not on any remote
    main_ahead: int = 0
    dirty_paths: int = 0
    stashes: int = 0

    @property
    def unpushed_work(self) -> bool:
        return bool(self.branches_ahead or self.main_ahead or self.dirty_paths or self.stashes)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def _log(repo: Path, *revspec: str) -> List[Commit]:
    out = _git(repo, "log", "--format=%H%x1f%cs%x1f%s", *revspec)
    commits = []
    for line in out.splitlines():
        sha, date, subject = line.split("\x1f", 2)
        commits.append(Commit(sha=sha[:9], date=date, subject=subject, where=""))
    return commits


def _index_by_key(commits: Iterable[Commit], where: str) -> Dict[str, List[Commit]]:
    by_key: Dict[str, List[Commit]] = {}
    for c in commits:
        c.where = where
        for key in set(KEY_RE.findall(c.subject)):
            by_key.setdefault(key, []).append(c)
    return by_key


def _merge(into: Dict[str, List[Commit]], more: Dict[str, List[Commit]]) -> None:
    for key, commits in more.items():
        into.setdefault(key, []).extend(commits)


def collect_repo(path: Path, shipped_ref: Optional[str] = None) -> RepoFacts:
    """Everything the host can see about one repository.

    ``shipped_ref`` defaults to origin/main when it exists, else main: "shipped"
    means other hosts can fetch it, which a local main cannot promise.
    """
    if shipped_ref is None:
        shipped_ref = "origin/main" if _git_ok(path, "rev-parse", "--verify", "-q",
                                              "origin/main") else "main"
    facts = RepoFacts(path=str(path), shipped_ref=shipped_ref)
    facts.shipped = _index_by_key(_log(path, shipped_ref), shipped_ref)

    # Commits on any local branch that no remote has. `--branches --not
    # --remotes` is the whole-repo answer; the per-branch loop below says where.
    _merge(facts.unpushed,
           _index_by_key(_log(path, "--branches", "--not", "--remotes"), "local-only"))
    for line in _git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        n = int(_git(path, "rev-list", "--count", line, "--not", "--remotes").strip() or 0)
        if n:
            facts.branches_ahead[line] = n
    if shipped_ref == "origin/main" and _git_ok(path, "rev-parse", "--verify", "-q", "main"):
        facts.main_ahead = int(_git(path, "rev-list", "--count", "origin/main..main").strip() or 0)
    facts.dirty_paths = len(_git(path, "status", "--porcelain").splitlines())
    facts.stashes = len(_git(path, "stash", "list").splitlines())
    return facts


# ── Reconciliation ───────────────────────────────────────────────────────────

@dataclass
class Finding:
    bucket: str
    key: str
    ticket_status: str
    detail: str
    commits: List[Commit] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)  # for triage: a bulk round-up closes in groups


@dataclass
class Report:
    project: str
    generated_at: str
    repos: List[RepoFacts]
    findings: List[Finding]

    def by_bucket(self, bucket: str) -> List[Finding]:
        return [f for f in self.findings if f.bucket == bucket]

    @property
    def drift(self) -> bool:
        return bool(self.findings) or any(r.unpushed_work for r in self.repos)


def reconcile(repos: Sequence[RepoFacts], source: TicketSource, *, project: str,
              now: datetime, min_age_days: int, since_days: int,
              ignore_labels: Sequence[str]) -> Report:
    shipped: Dict[str, List[Commit]] = {}
    unpushed: Dict[str, List[Commit]] = {}
    for r in repos:
        _merge(shipped, r.shipped)
        _merge(unpushed, r.unpushed)

    prefix = f"{project}-"
    shipped_keys = sorted(k for k in shipped if k.startswith(prefix))
    tickets = source.fetch(shipped_keys) if shipped_keys else {}
    findings: List[Finding] = []

    cutoff = (now - timedelta(days=min_age_days)).strftime("%Y-%m-%d")
    for key in shipped_keys:
        t = tickets.get(key)
        if t is None or t.category == DONE:
            continue
        newest = max(c.date for c in shipped[key])
        if newest > cutoff:
            continue  # in flight: landed within the grace window
        findings.append(Finding(
            bucket="shipped-but-open", key=key, ticket_status=t.status,
            detail=f"last commit {newest}: {shipped[key][0].subject[:80]}",
            commits=shipped[key][:3], labels=list(t.labels)))

    ignore = {label.lower() for label in ignore_labels}
    for t in source.closed_since(project, now - timedelta(days=since_days)):
        if t.issue_type == "Epic" or (t.resolution or "Done") != "Done":
            continue
        if ignore & {label.lower() for label in t.labels}:
            continue
        if t.key in shipped:
            continue
        if t.key in unpushed:
            findings.append(Finding(
                bucket="closed-but-unpushed", key=t.key, ticket_status=t.status,
                detail=f"{t.summary[:70]} | only on: "
                       + ", ".join(sorted({c.where for c in unpushed[t.key]})),
                commits=unpushed[t.key][:3], labels=list(t.labels)))
        else:
            findings.append(Finding(
                bucket="closed-but-unshipped", key=t.key, ticket_status=t.status,
                detail=f"resolved {t.resolved_at or '?'}: {t.summary[:70]}",
                labels=list(t.labels)))

    return Report(project=project, generated_at=now.isoformat(timespec="seconds"),
                  repos=list(repos), findings=findings)


# ── Presentation ─────────────────────────────────────────────────────────────

def render_text(report: Report) -> str:
    lines = [f"ticket/code reconcile — project {report.project} — {report.generated_at}", ""]
    for bucket, title in (
        ("shipped-but-open", "Shipped but ticket not Done"),
        ("closed-but-unpushed", "Ticket Done but the commit is only on this host"),
        ("closed-but-unshipped", "Ticket Done but no commit mentions it"),
    ):
        items = report.by_bucket(bucket)
        lines.append(f"{title}: {len(items)}")
        for f in items:
            tag = f"  [{', '.join(f.labels)}]" if f.labels else ""
            lines.append(f"  {f.key:<9} {f.ticket_status:<12} {f.detail}{tag}")
        lines.append("")
    lines.append("Unpushed work on this host:")
    for r in report.repos:
        bits = []
        if r.main_ahead:
            bits.append(f"main ahead of origin/main by {r.main_ahead}")
        for b, n in sorted(r.branches_ahead.items()):
            bits.append(f"{b} +{n}")
        if r.stashes:
            bits.append(f"{r.stashes} stash(es)")
        if r.dirty_paths:
            bits.append(f"{r.dirty_paths} dirty path(s)")
        lines.append(f"  {r.path} ({r.shipped_ref}): " + ("; ".join(bits) if bits else "clean"))
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


# ── Entry point ──────────────────────────────────────────────────────────────

def read_env_file(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines, without exporting them (DEV-508)."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def jira_from_env(env_file: Optional[Path]) -> AtlassianSource:
    values = read_env_file(env_file) if env_file else {}

    def get(name: str) -> str:
        v = os.environ.get(name) or values.get(name) or ""
        if not v:
            raise SystemExit(f"{name} is not set (environment or {env_file})")
        return v
    return AtlassianSource(get("JIRA_URL"), get("JIRA_EMAIL"), get("JIRA_API_TOKEN"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    here = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", action="append", type=Path,
                   help="repository to read (repeatable; default: this one)")
    p.add_argument("--project", default="DEV", help="Jira project key (default DEV)")
    p.add_argument("--shipped-ref", default=None,
                   help="ref that counts as shipped (default origin/main, else main)")
    p.add_argument("--min-age-days", type=int, default=2,
                   help="ignore shipped-but-open when the last commit is younger than this")
    p.add_argument("--since-days", type=int, default=90,
                   help="how far back to look for Done tickets")
    p.add_argument("--ignore-label", action="append", default=[],
                   help="skip Done tickets carrying this label (repeatable)")
    p.add_argument("--env-file", type=Path, default=here / ".env")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 1 on any drift")
    args = p.parse_args(argv)

    repos = [collect_repo(r.resolve(), args.shipped_ref) for r in (args.repo or [here])]
    report = reconcile(repos, jira_from_env(args.env_file), project=args.project,
                       now=datetime.now(timezone.utc), min_age_days=args.min_age_days,
                       since_days=args.since_days, ignore_labels=args.ignore_label)
    sys.stdout.write(render_json(report) if args.json else render_text(report))
    return 1 if (args.strict and report.drift) else 0


if __name__ == "__main__":
    sys.exit(main())
