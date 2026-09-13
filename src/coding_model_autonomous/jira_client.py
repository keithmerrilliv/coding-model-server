"""Jira client abstraction for autonomous-mode sync.

Two implementations land here:

- ``JiraClient`` (abstract) — the small surface the sync worker depends on.
- ``FakeJiraClient`` — in-memory; used by tests and as the default when
  no Jira credentials are configured. Records every call so tests can
  inspect state.
- ``AtlassianApiJiraClient`` — real client backed by ``atlassian-python-api``.
  Optional import; only constructed when JIRA_URL etc. are set.

Design notes:

- The interface uses *logical* status strings ("To Do", "In Progress",
  "In Review", "Done", "Rejected", "Cancelled"). Real Jira workflows can
  rename these, so the real client maps logical → workflow status when
  it transitions an issue. The fake just stores whatever it's given.
- Issue keys are opaque strings — for the fake, they look like
  ``AUTO-1``; for the real client, whatever Jira returns.
- The interface is *narrow on purpose*. We only need create/update/read,
  no fancy JQL or attachment handling, because the SQLite store remains
  the source of truth and Jira is just a projection.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from coding_model_autonomous.models import utc_now

logger = logging.getLogger(__name__)


# ── Logical status strings ───────────────────────────────────────────────────
# These match the default Jira Kanban workflow names. The real client maps
# them to whatever the live workflow uses; the fake stores them verbatim.

STATUS_TODO = "To Do"
STATUS_IN_PROGRESS = "In Progress"
STATUS_IN_REVIEW = "In Review"
STATUS_DONE = "Done"
STATUS_REJECTED = "Rejected"
STATUS_CANCELLED = "Cancelled"

# Fallback chain for workflows that don't define every logical status.
# Default Jira Cloud workflows only have To Do / In Progress / Done, so
# "In Review", "Rejected", and "Cancelled" have to collapse onto something
# real. The chain is tried in order — first hit wins.
STATUS_FALLBACKS: dict[str, list[str]] = {
    STATUS_TODO: [STATUS_TODO],
    STATUS_IN_PROGRESS: [STATUS_IN_PROGRESS],
    STATUS_IN_REVIEW: [STATUS_IN_REVIEW, STATUS_IN_PROGRESS],
    STATUS_DONE: [STATUS_DONE],
    STATUS_REJECTED: [STATUS_REJECTED, STATUS_DONE],
    STATUS_CANCELLED: [STATUS_CANCELLED, STATUS_DONE],
}

# DEV-482: the one collapse that lies. "Cancelled" (a FAILED or cancelled
# spec) and "Rejected" (a rejected gate) both fall back onto "Done" on a
# stock Jira Cloud workflow, and Done with resolution Done asserts success.
# When either lands on Done, the resolution carries the real outcome and a
# comment says so in words, so the audit trail can be read — and queried
# (`resolution = "Won't Do"`) — without knowing the fallback rules.
RESOLUTION_WONT_DO = "Won't Do"
LOSSY_COLLAPSES: frozenset = frozenset({STATUS_REJECTED, STATUS_CANCELLED})


def collapse_note(target_status: str, landed_status: str) -> str:
    """The comment left on an issue whose logical status the workflow
    cannot represent."""
    meaning = ("the spec FAILED or was cancelled" if target_status == STATUS_CANCELLED
               else "the review was REJECTED")
    return (f"**Mirror note:** {meaning}. This project's workflow has no "
            f"'{target_status}' status, so the issue is closed as "
            f"'{landed_status}' with resolution '{RESOLUTION_WONT_DO}'. "
            f"Do not read '{landed_status}' here as success (DEV-482).")

# Issue types
ISSUE_TYPE_EPIC = "Epic"
ISSUE_TYPE_STORY = "Story"
ISSUE_TYPE_TASK = "Task"
ISSUE_TYPE_SUBTASK = "Subtask"


# ── Plain data records ───────────────────────────────────────────────────────

@dataclass
class JiraIssue:
    """A Jira issue as the sync layer cares about it.

    We deliberately ignore most of Jira's surface area (custom fields,
    components, sprints, watchers, ...) because the SQLite store is the
    source of truth and we only need a projection.
    """
    key: str
    issue_type: str
    summary: str
    description: str
    status: str
    assignee: Optional[str] = None
    parent_key: Optional[str] = None  # epic key for stories under an epic
    resolution: Optional[str] = None  # DEV-482: "Won't Do" marks a lossy collapse
    comments: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


# ── Abstract interface ───────────────────────────────────────────────────────

class JiraClient(ABC):
    """Narrow interface the sync worker depends on. Implementations may
    add convenience methods, but the worker only uses the abstract ones.
    """

    @abstractmethod
    def create_epic(self, summary: str, description: str) -> str:
        """Create a top-level epic and return its issue key."""

    @abstractmethod
    def create_issue(
        self,
        summary: str,
        description: str,
        *,
        issue_type: str = ISSUE_TYPE_STORY,
        parent_epic_key: Optional[str] = None,
        assignee: Optional[str] = None,
    ) -> str:
        """Create a non-epic issue and return its key.

        If *parent_epic_key* is given, the issue is linked to that epic.
        """

    @abstractmethod
    def transition_issue(self, issue_key: str, target_status: str) -> Optional[str]:
        """Move an issue to a new workflow state. Returns the status the
        issue actually landed on — which differs from *target_status* when
        the workflow lacks it and a fallback was used (DEV-482)."""

    @abstractmethod
    def add_comment(self, issue_key: str, body: str) -> None:
        """Append a comment to an issue's comment thread."""

    @abstractmethod
    def get_issue(self, issue_key: str) -> Optional[JiraIssue]:
        """Read the current state of an issue, or None if it doesn't exist."""

    @abstractmethod
    def list_issues(self, *, parent_epic_key: Optional[str] = None) -> list[JiraIssue]:
        """List all issues the client knows about, optionally filtered to
        children of a specific epic. Used by the reverse-sync poll.
        """


# ── In-memory fake (default + tests) ─────────────────────────────────────────

class FakeJiraClient(JiraClient):
    """In-memory implementation. Records every call so tests can inspect.

    Status starts at ``STATUS_TODO`` for newly created issues. Tests
    drive the simulated reverse-sync (a human moving an issue in the
    Jira UI) by calling ``transition_issue`` directly.
    """

    def __init__(self, project_key: str = "AUTO",
                 statuses: "Optional[list[str]]" = None):
        self.project_key = project_key
        # DEV-482: the workflow's real statuses. None means every logical
        # status exists (the historical fake); a stock Jira Cloud workflow
        # is [To Do, In Progress, Done], and then Cancelled and Rejected
        # collapse exactly as they do against the real API.
        self.statuses: Optional[set[str]] = set(statuses) if statuses is not None else None
        self._issues: dict[str, JiraIssue] = {}
        self._next_id = 1
        # Audit log for tests — every API call appends here.
        self.call_log: list[tuple[str, dict]] = []

    # ── helpers ──────────────────────────────────────────────────────────────

    def _new_key(self) -> str:
        key = f"{self.project_key}-{self._next_id}"
        self._next_id += 1
        return key

    def _log(self, method: str, **kwargs) -> None:
        self.call_log.append((method, kwargs))

    # ── interface ────────────────────────────────────────────────────────────

    def create_epic(self, summary: str, description: str) -> str:
        key = self._new_key()
        self._issues[key] = JiraIssue(
            key=key,
            issue_type=ISSUE_TYPE_EPIC,
            summary=summary,
            description=description,
            status=STATUS_TODO,
        )
        self._log("create_epic", key=key, summary=summary)
        return key

    def create_issue(
        self,
        summary: str,
        description: str,
        *,
        issue_type: str = ISSUE_TYPE_STORY,
        parent_epic_key: Optional[str] = None,
        assignee: Optional[str] = None,
    ) -> str:
        key = self._new_key()
        self._issues[key] = JiraIssue(
            key=key,
            issue_type=issue_type,
            summary=summary,
            description=description,
            status=STATUS_TODO,
            assignee=assignee,
            parent_key=parent_epic_key,
        )
        self._log(
            "create_issue",
            key=key,
            issue_type=issue_type,
            parent_epic_key=parent_epic_key,
            summary=summary,
        )
        return key

    def transition_issue(self, issue_key: str, target_status: str) -> Optional[str]:
        if issue_key not in self._issues:
            raise KeyError(f"unknown issue {issue_key}")
        issue = self._issues[issue_key]
        landed = target_status
        if self.statuses is not None and target_status not in self.statuses:
            candidates = [c for c in STATUS_FALLBACKS.get(target_status, [target_status])
                          if c in self.statuses]
            if not candidates:
                raise RuntimeError(f"no usable transition for {target_status!r} on {issue_key}")
            landed = candidates[0]
        issue.status = landed
        issue.updated_at = utc_now()
        self._log("transition_issue", key=issue_key, target_status=target_status,
                  landed=landed)
        if landed != target_status and target_status in LOSSY_COLLAPSES:
            issue.resolution = RESOLUTION_WONT_DO
            self.add_comment(issue_key, collapse_note(target_status, landed))
            logger.warning("jira: %s has no %r status — %s closed as %r with "
                           "resolution %r (DEV-482)", self.project_key,
                           target_status, issue_key, landed, RESOLUTION_WONT_DO)
        return landed

    def add_comment(self, issue_key: str, body: str) -> None:
        if issue_key not in self._issues:
            raise KeyError(f"unknown issue {issue_key}")
        self._issues[issue_key].comments.append(body)
        self._issues[issue_key].updated_at = utc_now()
        self._log("add_comment", key=issue_key, body_chars=len(body))

    def get_issue(self, issue_key: str) -> Optional[JiraIssue]:
        return self._issues.get(issue_key)

    def list_issues(self, *, parent_epic_key: Optional[str] = None) -> list[JiraIssue]:
        if parent_epic_key is None:
            return list(self._issues.values())
        return [i for i in self._issues.values()
                if i.parent_key == parent_epic_key]

    # ── test helpers (not part of the interface) ─────────────────────────────

    def reset(self) -> None:
        """Wipe everything. Useful between test cases."""
        self._issues.clear()
        self._next_id = 1
        self.call_log.clear()


# ── Real Atlassian client (lazy import) ──────────────────────────────────────

class AtlassianApiJiraClient(JiraClient):
    """Real Jira client backed by atlassian-python-api.

    Lazy-imports the library so the daemon can run without it installed
    when only the fake is in use.
    """

    def __init__(
        self,
        url: str,
        email: str,
        api_token: str,
        project_key: str,
    ):
        try:
            from atlassian import Jira  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "atlassian-python-api is not installed; either pip-install "
                "it or stay on the FakeJiraClient by leaving JIRA_URL unset"
            ) from e

        # Defensive normalization — .env files commonly have trailing
        # whitespace and a trailing slash on URLs, both of which break
        # Atlassian REST routing in subtle ways.
        url = url.strip().rstrip("/")
        email = email.strip()
        api_token = api_token.strip()
        project_key = project_key.strip()

        self._jira = Jira(url=url, username=email, password=api_token, cloud=True)
        self.project_key = project_key
        logger.info("AtlassianApiJiraClient connected to %s (project=%s)",
                    url, project_key)

    def create_epic(self, summary: str, description: str) -> str:
        result = self._jira.create_issue(fields={
            "project": {"key": self.project_key},
            "summary": summary,
            "description": description,
            "issuetype": {"name": ISSUE_TYPE_EPIC},
        })
        return result["key"]

    def create_issue(
        self,
        summary: str,
        description: str,
        *,
        issue_type: str = ISSUE_TYPE_STORY,
        parent_epic_key: Optional[str] = None,
        assignee: Optional[str] = None,
    ) -> str:
        fields = {
            "project": {"key": self.project_key},
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
        }
        if parent_epic_key:
            # Cloud Jira uses the parent field for epic linking now
            fields["parent"] = {"key": parent_epic_key}
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        result = self._jira.create_issue(fields=fields)
        return result["key"]

    def transition_issue(self, issue_key: str, target_status: str) -> Optional[str]:
        # `set_issue_status` resolves target names to transition IDs by
        # walking available transitions, but returns None for names the
        # workflow doesn't define — then the library explodes trying to
        # POST None as the transition id. Resolve it ourselves so we can
        # (a) fall back to a nearby status and (b) skip no-op transitions
        # (Jira doesn't offer a self-loop and will reject them).
        current = self._jira.get_issue_status(issue_key)
        candidates = STATUS_FALLBACKS.get(target_status, [target_status])
        if current in candidates:
            if current != target_status and target_status in LOSSY_COLLAPSES:
                # Already sitting on the fallback (a Done from an earlier
                # mirror): the status cannot move, the resolution still can.
                self._mark_collapsed(issue_key, target_status, current, transition_id=None)
            return current

        transitions = self._jira.get_issue_transitions(issue_key)
        by_target = {t["to"]: t["id"] for t in transitions}
        for candidate in candidates:
            if candidate in by_target:
                if candidate != target_status and target_status in LOSSY_COLLAPSES:
                    self._mark_collapsed(issue_key, target_status, candidate,
                                         transition_id=by_target[candidate])
                else:
                    self._jira.set_issue_status_by_transition_id(
                        issue_key, by_target[candidate]
                    )
                return candidate

        raise RuntimeError(
            f"no usable transition for '{target_status}' "
            f"(current={current}) on {issue_key}. "
            f"Workflow offers: {sorted(by_target)}"
        )

    def _mark_collapsed(self, issue_key: str, target_status: str,
                        landed: str, *, transition_id: Optional[str]) -> None:
        """DEV-482: land on *landed* with resolution Won't Do, say so in a
        comment, and log it. The resolution is set on the transition itself
        where the workflow allows (that is the screen it lives on); when it
        does not, the transition still happens and the resolution is tried
        as a field edit, best-effort — the comment carries the truth either
        way."""
        fields = {"resolution": {"name": RESOLUTION_WONT_DO}}
        resolution_set = False
        if transition_id is not None:
            try:
                self._jira.set_issue_status(issue_key, landed, fields=fields)
                resolution_set = True
            except Exception as e:
                logger.warning("jira: transition of %s to %r with resolution "
                               "failed (%s); transitioning without it",
                               issue_key, landed, e)
                self._jira.set_issue_status_by_transition_id(issue_key, transition_id)
        if not resolution_set:
            try:
                self._jira.edit_issue(issue_key, fields, notify_users=False)
                resolution_set = True
            except Exception as e:
                logger.warning("jira: could not set resolution %r on %s (%s) — "
                               "the comment is the only marker", RESOLUTION_WONT_DO,
                               issue_key, e)
        try:
            self.add_comment(issue_key, collapse_note(target_status, landed))
        except Exception as e:
            logger.warning("jira: could not leave the collapse note on %s: %s",
                           issue_key, e)
        logger.warning("jira: workflow has no %r status — %s closed as %r with "
                       "resolution %s (DEV-482)", target_status, issue_key, landed,
                       RESOLUTION_WONT_DO if resolution_set else "UNSET")

    def add_comment(self, issue_key: str, body: str) -> None:
        self._jira.issue_add_comment(issue_key, body)

    def get_issue(self, issue_key: str) -> Optional[JiraIssue]:
        try:
            data = self._jira.issue(issue_key)
        except Exception:
            return None
        if not data:
            return None
        fields = data.get("fields", {})
        resolution = fields.get("resolution") or {}
        return JiraIssue(
            key=data["key"],
            issue_type=fields.get("issuetype", {}).get("name", ""),
            resolution=resolution.get("name") if isinstance(resolution, dict) else None,
            summary=fields.get("summary", ""),
            description=fields.get("description") or "",
            status=fields.get("status", {}).get("name", ""),
            assignee=(fields.get("assignee") or {}).get("displayName"),
            parent_key=(fields.get("parent") or {}).get("key"),
            comments=[c.get("body", "") for c in
                      fields.get("comment", {}).get("comments", [])],
        )

    def list_issues(self, *, parent_epic_key: Optional[str] = None) -> list[JiraIssue]:
        if parent_epic_key:
            jql = f'project = "{self.project_key}" AND parent = "{parent_epic_key}"'
        else:
            jql = f'project = "{self.project_key}"'
        response = self._jira.jql(jql)
        # The atlassian-python-api library returns the parsed JSON as-is.
        # An error response from Jira looks like {"errorMessages": [...],
        # "errors": {...}} with no "issues" key at all. Returning an empty
        # list there would silently swallow auth/access failures and let
        # the sync worker think it had nothing to do, so we surface it.
        if not isinstance(response, dict) or "issues" not in response:
            error_msg = (
                response.get("errorMessages", ["unknown error"])[0]
                if isinstance(response, dict) else str(response)
            )
            raise RuntimeError(
                f"Jira JQL response did not include 'issues' key — likely "
                f"an auth or permission failure. Response: {error_msg}"
            )
        out: list[JiraIssue] = []
        for r in response["issues"]:
            f = r.get("fields", {})
            out.append(JiraIssue(
                key=r["key"],
                issue_type=f.get("issuetype", {}).get("name", ""),
                summary=f.get("summary", ""),
                description=f.get("description") or "",
                status=f.get("status", {}).get("name", ""),
                assignee=(f.get("assignee") or {}).get("displayName"),
                parent_key=(f.get("parent") or {}).get("key"),
            ))
        return out
