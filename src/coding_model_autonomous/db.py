"""SQLite-backed task store for coding_model_autonomous.

Single source of truth for autonomous-mode state. The orchestrator daemon and
the FastAPI server both import this module — connections are thread-safe via
WAL mode and per-thread connection objects.

The schema lives in schema.sql and is applied idempotently on first connect.

ID format: ``<prefix>_<8 hex chars>``, e.g. ``spec_a3f9b2c1``. Short enough to
type in CLI commands, long enough to avoid collisions in this single-user
deployment (8 hex = 4.3B values).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from coding_model_autonomous.models import (
    Artifact,
    ArtifactKind,
    Event,
    EventKind,
    GateStatus,
    GateType,
    ReviewGate,
    Spec,
    SpecStatus,
    Task,
    TaskStatus,
    utc_now,
)

logger = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────────────
# Resolved relative to the repo root unless CODING_MODEL_TASKS_DB / CODING_MODEL_TASKS_WORKSPACE
# are set in the environment.

# This file lives at src/coding_model_autonomous/db.py — three parents up reaches
# the repo root. Runtime state lives under var/ (see var/tasks_db/).
_REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DB_PATH = Path(
    os.getenv("CODING_MODEL_TASKS_DB", _REPO_ROOT / "var" / "tasks_db" / "tasks.sqlite")
)
DEFAULT_WORKSPACE_ROOT = Path(
    os.getenv("CODING_MODEL_TASKS_WORKSPACE", _REPO_ROOT / "var" / "tasks_db" / "specs")
)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    return datetime.fromisoformat(s)


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """Thin wrapper around a SQLite connection.

    Each thread that calls into this class gets its own connection from a
    thread-local cache; SQLite is happy with concurrent reads under WAL.
    Writes serialize naturally because there's only one writer at a time
    (orchestrator daemon for state mutations, server for spec submissions).
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH,
                 workspace_root: Path = DEFAULT_WORKSPACE_ROOT):
        self.db_path = Path(db_path)
        self.workspace_root = Path(workspace_root)
        self._tls = threading.local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._bootstrap_schema()

    # ── connection management ────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit; transactions are explicit
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # Retry rather than fail immediately when another connection holds
            # the write lock — the daemon and the FastAPI server each hold their
            # own connection to this DB. Paired with BEGIN IMMEDIATE below: an
            # IMMEDIATE that can't get the lock returns plain SQLITE_BUSY, which
            # the busy handler retries; a DEFERRED that upgrades mid-transaction
            # returns SQLITE_BUSY_SNAPSHOT, which the busy handler does NOT retry.
            conn.execute("PRAGMA busy_timeout = 5000")
            self._tls.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE, not plain BEGIN (== DEFERRED). Every use site here is
        # a writer, and several read-then-write (e.g. respond_to_gate SELECTs the
        # gate then UPDATEs it). Under WAL a DEFERRED txn takes only a read lock
        # at BEGIN and tries to upgrade on the first write; if another connection
        # wrote in between, the upgrade fails with SQLITE_BUSY_SNAPSHOT, which is
        # NOT retried by busy_timeout and surfaces as "database is locked".
        # IMMEDIATE takes the write lock up front, so contention degrades to a
        # retryable wait instead of a hard error. Read-only paths don't use
        # transaction() (they execute() directly), so none are penalized.
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close_all(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None

    def _bootstrap_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text()
        conn = self._conn()
        conn.executescript(sql)

    # ── spec workspace helpers ───────────────────────────────────────────────

    def spec_dir(self, spec_id: str) -> Path:
        d = self.workspace_root / spec_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── specs ────────────────────────────────────────────────────────────────

    def create_spec(self, title: str, source_md_path: str,
                    status: SpecStatus = SpecStatus.PENDING_PLAN) -> Spec:
        spec_id = _new_id("spec")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO specs (id, title, source_md_path, normalized_yaml,
                                   status, jira_epic_key, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (spec_id, title, source_md_path, status.value, _iso(now), _iso(now)),
            )
            self._record_event(
                conn,
                EventKind.SPEC_SUBMITTED,
                spec_id=spec_id,
                payload={"title": title, "source_md_path": source_md_path},
            )
        return Spec(
            id=spec_id,
            title=title,
            source_md_path=source_md_path,
            normalized_yaml=None,
            status=status,
            jira_epic_key=None,
            created_at=now,
            updated_at=now,
        )

    def get_spec(self, spec_id: str) -> Optional[Spec]:
        row = self._conn().execute(
            "SELECT * FROM specs WHERE id = ?", (spec_id,)
        ).fetchone()
        return _row_to_spec(row) if row else None

    def list_specs(self, status: Optional[SpecStatus] = None,
                   limit: int = 100) -> list[Spec]:
        if status is None:
            rows = self._conn().execute(
                "SELECT * FROM specs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM specs WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [_row_to_spec(r) for r in rows]

    def update_spec_status(self, spec_id: str, status: SpecStatus,
                           normalized_yaml: Optional[str] = None) -> None:
        now = utc_now()
        with self.transaction() as conn:
            if normalized_yaml is not None:
                conn.execute(
                    "UPDATE specs SET status = ?, normalized_yaml = ?, "
                    "updated_at = ? WHERE id = ?",
                    (status.value, normalized_yaml, _iso(now), spec_id),
                )
            else:
                conn.execute(
                    "UPDATE specs SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, _iso(now), spec_id),
                )
            self._record_event(
                conn,
                EventKind.SPEC_STATUS_CHANGED,
                spec_id=spec_id,
                payload={"new_status": status.value},
            )

    def set_spec_jira_epic(self, spec_id: str, epic_key: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE specs SET jira_epic_key = ?, updated_at = ? WHERE id = ?",
                (epic_key, _iso(utc_now()), spec_id),
            )

    def set_gate_jira_issue(self, gate_id: str, issue_key: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE review_gates SET jira_issue_key = ? WHERE id = ?",
                (issue_key, gate_id),
            )

    def set_task_jira_issue(self, task_id: str, issue_key: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET jira_issue_key = ?, updated_at = ? WHERE id = ?",
                (issue_key, _iso(utc_now()), task_id),
            )

    # ── tasks ────────────────────────────────────────────────────────────────

    def create_task(self, *, spec_id: str, agent: str, role: str, title: str,
                    description: Optional[str] = None,
                    parent_id: Optional[str] = None,
                    execution_target: Optional[str] = None,
                    status: TaskStatus = TaskStatus.PENDING) -> Task:
        task_id = _new_id("task")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, spec_id, parent_id, agent, role, title,
                                   description, status, execution_target,
                                   jira_issue_key, started_at, completed_at,
                                   retry_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?)
                """,
                (task_id, spec_id, parent_id, agent, role, title, description,
                 status.value, execution_target, _iso(now), _iso(now)),
            )
            self._record_event(
                conn,
                EventKind.TASK_CREATED,
                spec_id=spec_id,
                task_id=task_id,
                payload={"agent": agent, "role": role, "title": title},
            )
        return Task(
            id=task_id, spec_id=spec_id, parent_id=parent_id, agent=agent,
            role=role, title=title, description=description, status=status,
            execution_target=execution_target, jira_issue_key=None,
            started_at=None, completed_at=None, retry_count=0,
            created_at=now, updated_at=now,
        )

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self._conn().execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks_for_spec(self, spec_id: str) -> list[Task]:
        rows = self._conn().execute(
            "SELECT * FROM tasks WHERE spec_id = ? ORDER BY created_at",
            (spec_id,),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def count_tasks_for_spec(self, spec_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM tasks WHERE spec_id = ?", (spec_id,)
        ).fetchone()
        return row[0]

    def increment_task_retry(self, task_id: str) -> None:
        """Atomically bump retry_count for a task."""
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET retry_count = retry_count + 1, "
                "updated_at = ? WHERE id = ?",
                (_iso(now), task_id),
            )

    def update_task_agent(self, task_id: str, agent: str) -> None:
        """Override the agent recorded for a task.

        Used when the architect's complexity assessment routes the implementer
        task to a non-default agent. Keeping the DB record authoritative
        matters for telemetry queries ("which agent built spec X?").
        """
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET agent = ?, updated_at = ? WHERE id = ?",
                (agent, _iso(now), task_id),
            )

    def list_tasks_for_spec_by_role(self, spec_id: str,
                                     role: str) -> list[Task]:
        rows = self._conn().execute(
            "SELECT * FROM tasks WHERE spec_id = ? AND role = ? "
            "ORDER BY created_at",
            (spec_id, role),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        now = utc_now()
        started_clause = ""
        completed_clause = ""
        params: list = [status.value, _iso(now)]
        if status == TaskStatus.RUNNING:
            started_clause = ", started_at = COALESCE(started_at, ?)"
            params.append(_iso(now))
        if status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED):
            completed_clause = ", completed_at = ?"
            params.append(_iso(now))
        params.append(task_id)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE tasks SET status = ?, updated_at = ?{started_clause}"
                f"{completed_clause} WHERE id = ?",
                params,
            )
            row = conn.execute(
                "SELECT spec_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self._record_event(
                conn,
                EventKind.TASK_STATUS_CHANGED,
                spec_id=row["spec_id"] if row else None,
                task_id=task_id,
                payload={"new_status": status.value},
            )

    # ── artifacts ────────────────────────────────────────────────────────────

    def create_artifact(self, *, spec_id: str, kind: ArtifactKind, path: str,
                        task_id: Optional[str] = None,
                        sha256: Optional[str] = None) -> Artifact:
        art_id = _new_id("artifact")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (id, spec_id, task_id, kind, path,
                                       sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (art_id, spec_id, task_id, kind.value, path, sha256, _iso(now)),
            )
            self._record_event(
                conn,
                EventKind.ARTIFACT_CREATED,
                spec_id=spec_id,
                task_id=task_id,
                payload={"kind": kind.value, "path": path},
            )
        return Artifact(
            id=art_id, spec_id=spec_id, task_id=task_id, kind=kind,
            path=path, sha256=sha256, created_at=now,
        )

    def list_artifacts(self, spec_id: str,
                       kind: Optional[ArtifactKind] = None) -> list[Artifact]:
        """All artifacts for a spec, oldest first; optionally filtered by kind."""
        if kind is None:
            rows = self._conn().execute(
                "SELECT * FROM artifacts WHERE spec_id = ? ORDER BY created_at",
                (spec_id,),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM artifacts WHERE spec_id = ? AND kind = ? "
                "ORDER BY created_at",
                (spec_id, kind.value),
            ).fetchall()
        return [_row_to_artifact(r) for r in rows]

    # ── review gates ─────────────────────────────────────────────────────────

    def create_gate(self, *, spec_id: str, gate_type: GateType, prompt_md: str,
                    task_id: Optional[str] = None) -> ReviewGate:
        gate_id = _new_id("gate")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_gates (id, spec_id, task_id, gate_type,
                                          prompt_md, status, reviewer_decision,
                                          reviewer_notes, jira_issue_key,
                                          created_at, responded_at)
                VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, NULL)
                """,
                (gate_id, spec_id, task_id, gate_type.value, prompt_md, _iso(now)),
            )
            self._record_event(
                conn,
                EventKind.GATE_CREATED,
                spec_id=spec_id,
                task_id=task_id,
                gate_id=gate_id,
                payload={"gate_type": gate_type.value},
            )
        return ReviewGate(
            id=gate_id, spec_id=spec_id, task_id=task_id, gate_type=gate_type,
            prompt_md=prompt_md, status=GateStatus.PENDING,
            reviewer_decision=None, reviewer_notes=None, jira_issue_key=None,
            created_at=now, responded_at=None,
        )

    def get_gate(self, gate_id: str) -> Optional[ReviewGate]:
        row = self._conn().execute(
            "SELECT * FROM review_gates WHERE id = ?", (gate_id,)
        ).fetchone()
        return _row_to_gate(row) if row else None

    def list_open_gates(self, spec_id: Optional[str] = None) -> list[ReviewGate]:
        if spec_id is None:
            rows = self._conn().execute(
                "SELECT * FROM review_gates WHERE status = 'pending' "
                "ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM review_gates WHERE status = 'pending' "
                "AND spec_id = ? ORDER BY created_at",
                (spec_id,),
            ).fetchall()
        return [_row_to_gate(r) for r in rows]

    def list_gates_for_spec(self, spec_id: str,
                            gate_type: Optional[GateType] = None) -> list[ReviewGate]:
        """All gates for a spec in chronological order, open or closed.

        Used by the planner re-run path to gather every clarification round
        in the order it happened so the planner sees full context.
        """
        if gate_type is None:
            rows = self._conn().execute(
                "SELECT * FROM review_gates WHERE spec_id = ? "
                "ORDER BY created_at",
                (spec_id,),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM review_gates WHERE spec_id = ? "
                "AND gate_type = ? ORDER BY created_at",
                (spec_id, gate_type.value),
            ).fetchall()
        return [_row_to_gate(r) for r in rows]

    def respond_to_gate(self, gate_id: str, decision: str,
                        notes: Optional[str] = None) -> ReviewGate:
        if decision not in ("approved", "rejected"):
            raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
        now = utc_now()
        new_status = (GateStatus.APPROVED if decision == "approved"
                      else GateStatus.REJECTED)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT spec_id, task_id FROM review_gates WHERE id = ?",
                (gate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no gate {gate_id}")
            conn.execute(
                "UPDATE review_gates SET status = ?, reviewer_decision = ?, "
                "reviewer_notes = ?, responded_at = ? WHERE id = ?",
                (new_status.value, decision, notes, _iso(now), gate_id),
            )
            self._record_event(
                conn,
                EventKind.GATE_RESPONDED,
                spec_id=row["spec_id"],
                task_id=row["task_id"],
                gate_id=gate_id,
                payload={"decision": decision, "notes": notes},
            )
        gate = self.get_gate(gate_id)
        assert gate is not None  # we just verified existence above
        return gate

    # ── events ───────────────────────────────────────────────────────────────

    def _record_event(self, conn: sqlite3.Connection, kind: EventKind, *,
                      spec_id: Optional[str] = None,
                      task_id: Optional[str] = None,
                      gate_id: Optional[str] = None,
                      payload: Optional[dict] = None) -> None:
        """Insert one event row inside an existing transaction."""
        conn.execute(
            """
            INSERT INTO events (spec_id, task_id, gate_id, kind,
                                payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (spec_id, task_id, gate_id, kind.value,
             json.dumps(payload) if payload else None,
             _iso(utc_now())),
        )

    def record_event(self, kind: EventKind, *,
                     spec_id: Optional[str] = None,
                     task_id: Optional[str] = None,
                     gate_id: Optional[str] = None,
                     payload: Optional[dict] = None) -> None:
        """Public event-recording helper for callers without an open txn."""
        with self.transaction() as conn:
            self._record_event(conn, kind, spec_id=spec_id, task_id=task_id,
                               gate_id=gate_id, payload=payload)

    def list_recent_events(self, spec_id: Optional[str] = None,
                           limit: int = 50) -> list[Event]:
        if spec_id is None:
            rows = self._conn().execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM events WHERE spec_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (spec_id, limit),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def count_events(self, *, spec_id: str, kind: EventKind) -> int:
        """Count events of *kind* for a spec. Used for supervisor budget."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM events WHERE spec_id = ? AND kind = ?",
            (spec_id, kind.value),
        ).fetchone()
        return int(row["n"]) if row else 0

    def list_events_by_kind(self, *, spec_id: str, kind: EventKind,
                            limit: int = 20) -> list[Event]:
        """Most-recent-first list of events of *kind* for a spec."""
        rows = self._conn().execute(
            "SELECT * FROM events WHERE spec_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT ?",
            (spec_id, kind.value, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def events_after(self, last_seen_id: int, limit: int = 100) -> list[Event]:
        """Used by sync workers to get events they haven't processed yet."""
        rows = self._conn().execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (last_seen_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    # ── sync_state (background worker bookkeeping) ───────────────────────────

    def get_sync_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn().execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_sync_state(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, _iso(utc_now())),
            )


# ── row → model conversion ───────────────────────────────────────────────────

def _row_to_spec(row: sqlite3.Row) -> Spec:
    return Spec(
        id=row["id"],
        title=row["title"],
        source_md_path=row["source_md_path"],
        normalized_yaml=row["normalized_yaml"],
        status=SpecStatus(row["status"]),
        jira_epic_key=row["jira_epic_key"],
        created_at=_parse_iso(row["created_at"]),
        updated_at=_parse_iso(row["updated_at"]),
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        spec_id=row["spec_id"],
        parent_id=row["parent_id"],
        agent=row["agent"],
        role=row["role"],
        title=row["title"],
        description=row["description"],
        status=TaskStatus(row["status"]),
        execution_target=row["execution_target"],
        jira_issue_key=row["jira_issue_key"],
        started_at=_parse_iso(row["started_at"]),
        completed_at=_parse_iso(row["completed_at"]),
        retry_count=row["retry_count"],
        created_at=_parse_iso(row["created_at"]),
        updated_at=_parse_iso(row["updated_at"]),
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        spec_id=row["spec_id"],
        task_id=row["task_id"],
        kind=ArtifactKind(row["kind"]),
        path=row["path"],
        sha256=row["sha256"],
        created_at=_parse_iso(row["created_at"]),
    )


def _row_to_gate(row: sqlite3.Row) -> ReviewGate:
    return ReviewGate(
        id=row["id"],
        spec_id=row["spec_id"],
        task_id=row["task_id"],
        gate_type=GateType(row["gate_type"]),
        prompt_md=row["prompt_md"],
        status=GateStatus(row["status"]),
        reviewer_decision=row["reviewer_decision"],
        reviewer_notes=row["reviewer_notes"],
        jira_issue_key=row["jira_issue_key"],
        created_at=_parse_iso(row["created_at"]),
        responded_at=_parse_iso(row["responded_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        spec_id=row["spec_id"],
        task_id=row["task_id"],
        gate_id=row["gate_id"],
        kind=EventKind(row["kind"]),
        payload_json=row["payload_json"],
        created_at=_parse_iso(row["created_at"]),
    )
