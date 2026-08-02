-- coding_model_autonomous schema (SQLite)
--
-- Append-only event log is the source of truth for crash recovery; the other
-- tables are projections that can be rebuilt from events if needed.
--
-- All timestamps are ISO8601 UTC strings (no SQLite DATETIME — explicit text
-- so JSON round-trips are lossless).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- One row per autonomous job (one markdown spec submission).
CREATE TABLE IF NOT EXISTS specs (
    id              TEXT PRIMARY KEY,         -- spec_<8 hex>
    title           TEXT NOT NULL,
    source_md_path  TEXT NOT NULL,            -- relative to the spec's workspace dir (workspace_root/<spec_id>/)
    normalized_yaml TEXT,                     -- NULL until planner produces YAML
    status          TEXT NOT NULL,            -- SpecStatus enum value
    jira_epic_key   TEXT,                     -- populated by Phase 1c
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Nodes in the task DAG. Phase 1a stores them; Phase 2 executes them.
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,       -- task_<8 hex>
    spec_id           TEXT NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
    parent_id         TEXT REFERENCES tasks(id),
    agent             TEXT NOT NULL,          -- agent name (planner, architect, ...)
    role              TEXT NOT NULL,          -- semantic role (planner, architect, ...)
    title             TEXT NOT NULL,
    description       TEXT,
    status            TEXT NOT NULL,          -- TaskStatus enum value
    execution_target  TEXT,                   -- 'server' or 'client:<name>' (Phase 2)
    jira_issue_key    TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Files produced by agents (design docs, code diffs, test reports, ...).
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,             -- artifact_<8 hex>
    spec_id     TEXT NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
    task_id     TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                -- ArtifactKind enum value
    path        TEXT NOT NULL,                -- relative to the spec's workspace dir (workspace_root/<spec_id>/)
    sha256      TEXT,
    created_at  TEXT NOT NULL
);

-- Pending human review gates.
CREATE TABLE IF NOT EXISTS review_gates (
    id                  TEXT PRIMARY KEY,     -- gate_<8 hex>
    spec_id             TEXT NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
    task_id             TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    gate_type           TEXT NOT NULL,        -- GateType enum value
    prompt_md           TEXT NOT NULL,        -- markdown the reviewer sees
    status              TEXT NOT NULL,        -- GateStatus enum value
    reviewer_decision   TEXT,                 -- 'approved' / 'rejected' / NULL
    reviewer_notes      TEXT,                 -- markdown reply
    jira_issue_key      TEXT,
    created_at          TEXT NOT NULL,
    responded_at        TEXT
);

-- Bookkeeping for background sync workers. Each worker stores its
-- "last processed event id" (and any other state it needs) here as a
-- key-value row, so it can resume from where it left off after a crash.
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Append-only audit log. Crash recovery replays from here.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id       TEXT REFERENCES specs(id) ON DELETE CASCADE,
    task_id       TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    gate_id       TEXT REFERENCES review_gates(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL,              -- EventKind enum value
    payload_json  TEXT,                       -- JSON, may be NULL
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_spec    ON tasks(spec_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_gates_status  ON review_gates(status);
CREATE INDEX IF NOT EXISTS idx_gates_spec    ON review_gates(spec_id);
CREATE INDEX IF NOT EXISTS idx_events_spec   ON events(spec_id);
CREATE INDEX IF NOT EXISTS idx_events_kind   ON events(kind);
