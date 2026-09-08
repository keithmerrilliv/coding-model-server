"""Artifact ledger — the one door every artifact write goes through (DEV-642).

Phase 1 of the pipeline-kernel refactor. Before it, the daemon wrote
artifacts through sixteen ad-hoc calls, none of which knew who had produced
the file already there, what the repository version looked like, or what
the build check had certified. That is how run 17 shipped a 6-line reviewer
stub over a 419-line test suite (DEV-602), how run 21 offered a 61-line
daemon in place of 6,130 lines with green tests (DEV-636), and how run 21's
synthesis prompt carried 79 sandbox-overlay files as "attempt code"
(DEV-639).

``ArtifactLedger.write`` is now the only way a role's output reaches the
workspace. Every write records role, kind, retry, sha256, size and the design
digest it was written against, both in the artifacts table and in a sidecar
``ledger.json`` that survives retry cleanup and is copied into every
``retry_history`` snapshot — so the synthesis corpus can be built from what
each attempt actually wrote. Three guards run before the bytes land:

* **collision** — a role writing at a path another role produced (and whose
  content is still on disk) is handled by policy: ``rename`` (default) moves
  the write to a sibling path that keeps test discovery working, ``refuse``
  drops it. Synthesis and its repair round may supersede implementer output;
  that is what they exist for.
* **emptying** — replacing a file that has declarations with one that has
  none is refused for every role (DEV-602 fix 3, DEV-573's stub class).
* **shrink** — when the repository version of the path is known (recorded
  whenever a role's existing-file fetch returned it), a write with fewer than
  ``SHRINK_REFUSE_RATIO`` of its lines AND declarations is refused (DEV-636).

Refusals and renames are returned as ``WriteOutcome`` values and persisted as
ledger entries, so the caller can put them on the gate and the event.
Diagnostics (test output, failure reports, build logs) go through ``note``:
traversal-protected, unguarded, unrecorded — they are not artifacts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from .executor import _count_declarations, artifact_path
from .models import ArtifactKind

logger = logging.getLogger("orchestrator.workspace")

LEDGER_FILE = "ledger.json"


class CollisionPolicy(str, Enum):
    RENAME = "rename"
    REFUSE = "refuse"


def _policy_from_env() -> CollisionPolicy:
    raw = os.getenv("AUTONOMOUS_COLLISION_POLICY", "rename").strip().lower()
    try:
        return CollisionPolicy(raw)
    except ValueError:
        logger.warning("AUTONOMOUS_COLLISION_POLICY=%r is not rename|refuse — "
                       "using rename", raw)
        return CollisionPolicy.RENAME


COLLISION_POLICY = _policy_from_env()
# A write is a shrink when its lines AND declarations are both below this
# fraction of the repository version's (DEV-636: 61 lines for 6,130).
SHRINK_REFUSE_RATIO = float(os.getenv("AUTONOMOUS_SHRINK_REFUSE_RATIO", "0.25"))
# Baselines smaller than this are never shrink-checked — a 12-line file
# rewritten as 3 lines is an edit, not a stub.
SHRINK_MIN_BASELINE_LINES = int(os.getenv("AUTONOMOUS_SHRINK_MIN_BASELINE_LINES", "40"))

# Roles whose output legitimately replaces another role's at the same path.
# Synthesis merges the implementer's attempts; the repair round patches the
# synthesis. Everything else at a foreign path is a collision.
_MAY_SUPERSEDE: dict[str, frozenset[str]] = {
    "synthesizer": frozenset({"implementer", "synthesizer", "synthesis_repair"}),
    "synthesis_repair": frozenset({"implementer", "synthesizer", "synthesis_repair"}),
}

ACTION_WRITTEN = "written"
ACTION_RENAMED = "renamed"
ACTION_RESTORED = "restored"
REFUSED_COLLISION = "refused_collision"
REFUSED_EMPTYING = "refused_emptying"
REFUSED_SHRINK = "refused_shrink"
REFUSALS = (REFUSED_COLLISION, REFUSED_EMPTYING, REFUSED_SHRINK)
_LANDED = (ACTION_WRITTEN, ACTION_RENAMED, ACTION_RESTORED)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _line_count(content: str) -> int:
    return len(content.splitlines())


def design_digest_of(spec_dir: Path) -> str:
    """sha1[:12] of design.md — the same digest retry_policy stamps on
    snapshots, so ledger entries and DEV-553's staleness check agree."""
    design = spec_dir / "design.md"
    if not design.is_file():
        return ""
    try:
        return hashlib.sha1(design.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


@dataclass
class Entry:
    """One write (landed or refused), as persisted in ledger.json."""
    path: str                 # where it landed, or the requested path when refused
    role: str
    kind: str
    action: str
    sha256: str
    lines: int
    decls: int
    retry: int = 0
    task_id: Optional[str] = None
    design_digest: str = ""
    requested: Optional[str] = None   # original path when renamed/refused
    prior_role: Optional[str] = None
    detail: str = ""
    at: str = ""

    @property
    def landed(self) -> bool:
        return self.action in _LANDED


@dataclass
class Baseline:
    """The repository version of a path at base_ref, as a role fetched it."""
    path: str
    sha256: str
    lines: int
    decls: int


@dataclass
class WriteOutcome:
    requested: str
    path: Optional[str]       # None when refused
    action: str
    role: str
    prior_role: Optional[str] = None
    detail: str = ""
    sha256: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.path is not None

    @property
    def refused(self) -> bool:
        return self.action in REFUSALS

    def describe(self) -> str:
        if self.action == ACTION_RENAMED:
            return (f"`{self.requested}` was produced by the {self.prior_role}; "
                    f"the {self.role}'s version was written as `{self.path}` "
                    f"instead (collision policy: rename)")
        if self.action == REFUSED_COLLISION:
            return (f"`{self.requested}` was produced by the {self.prior_role}; "
                    f"the {self.role}'s version was REFUSED (collision policy: refuse)")
        if self.action == REFUSED_EMPTYING:
            return (f"`{self.requested}`: the {self.role}'s version has no "
                    f"declarations where the current file has some — REFUSED "
                    f"({self.detail})")
        if self.action == REFUSED_SHRINK:
            return (f"`{self.requested}`: the {self.role}'s version is a fraction "
                    f"of the repository file — REFUSED ({self.detail})")
        return f"`{self.path}` written by the {self.role}"


def renamed_path(rel_path: str, role: str) -> str:
    """Sibling path for a colliding write that keeps test discovery working.

    ``tests/test_x.py`` → ``tests/test_reviewer_x.py`` (pytest collects
    ``test_*.py`` only); ``Dir/Foo.swift`` → ``Dir/reviewer_Foo.swift`` (Swift
    does not care what the file is called, only which target dir it is in).
    """
    p = PurePosixPath(rel_path)
    name = p.name
    if name.startswith("test_"):
        new = f"test_{role}_{name[len('test_'):]}"
    else:
        new = f"{role}_{name}"
    return str(p.with_name(new))


class ArtifactLedger:
    """Per-spec record of every artifact write, and the guards on the door."""

    def __init__(self, db: Any, spec_id: str, spec_dir: Path, *,
                 policy: "CollisionPolicy | None" = None) -> None:
        self.db = db
        self.spec_id = spec_id
        self.spec_dir = Path(spec_dir)
        self.policy = policy or COLLISION_POLICY
        self.entries: list[Entry] = []
        self.baselines: dict[str, Baseline] = {}
        self.outcomes: list[WriteOutcome] = []   # this instance's writes, in order
        self._load()

    @classmethod
    def open(cls, db: Any, spec: Any, spec_dir: "Path | None" = None,
             **kw: Any) -> "ArtifactLedger":
        return cls(db, spec.id, spec_dir or db.spec_dir(spec.id), **kw)

    # ── persistence ──────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self.spec_dir / LEDGER_FILE

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
            self.entries = [Entry(**e) for e in data.get("entries", [])]
            self.baselines = {p: Baseline(**b) for p, b in
                              (data.get("baselines") or {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("spec %s: %s unreadable (%s) — starting an empty "
                           "ledger; prior writes will not be seen as producers",
                           self.spec_id, LEDGER_FILE, exc)
            self.entries, self.baselines = [], {}

    def _save(self) -> None:
        payload = {"entries": [asdict(e) for e in self.entries],
                   "baselines": {p: asdict(b) for p, b in self.baselines.items()}}
        try:
            self.spec_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=1))
        except OSError as exc:
            logger.error("spec %s: could not persist %s: %s",
                         self.spec_id, LEDGER_FILE, exc)

    # ── baselines (the repository version) ───────────────────────────────────

    def record_baseline(self, files: Iterable[tuple[str, str]]) -> int:
        """Remember what the repository holds at these paths (from a fetch)."""
        n = 0
        for rel_path, content in files:
            if not isinstance(content, str):
                continue
            self.baselines[rel_path] = Baseline(
                rel_path, sha256_text(content), _line_count(content),
                _count_declarations(content))
            n += 1
        if n:
            self._save()
        return n

    def baseline(self, rel_path: str) -> "Baseline | None":
        return self.baselines.get(rel_path)

    # ── producers ────────────────────────────────────────────────────────────

    def _disk_sha(self, rel_path: str) -> "str | None":
        try:
            p = artifact_path(self.spec_dir, rel_path)
        except ValueError:
            return None
        if not p.is_file():
            return None
        try:
            return hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            return None

    def producer(self, rel_path: str) -> "Entry | None":
        """The entry whose bytes are currently at *rel_path*, if any.

        A file that was wiped by retry cleanup, or changed by something the
        ledger never saw, has no producer — nobody's work is at stake there.
        """
        on_disk = self._disk_sha(rel_path)
        if on_disk is None:
            return None
        for e in reversed(self.entries):
            if e.path == rel_path and e.landed:
                return e if e.sha256 == on_disk else None
        return None

    # ── the door ─────────────────────────────────────────────────────────────

    def write(self, rel_path: str, content: str, *, role: str,
              kind: ArtifactKind = ArtifactKind.CODE,
              task_id: "str | None" = None, retry: int = 0) -> WriteOutcome:
        """Write one artifact, subject to the guards. Never raises on a guard;
        a traversal path raises ValueError like artifact_path always did."""
        target = rel_path
        prior = self.producer(rel_path)
        prior_role = prior.role if prior else None
        outcome: "WriteOutcome | None" = None

        # 1. collision — someone else's work is at this path.
        if prior is not None and prior.role != role \
                and prior.role not in _MAY_SUPERSEDE.get(role, frozenset()):
            if self.policy is CollisionPolicy.RENAME:
                candidate = renamed_path(rel_path, role)
                other = self.producer(candidate)
                if other is not None and other.role != role:
                    outcome = WriteOutcome(rel_path, None, REFUSED_COLLISION, role,
                                           prior_role, detail=(
                                               f"rename target `{candidate}` is "
                                               f"also held by the {other.role}"))
                else:
                    target = candidate
            else:
                outcome = WriteOutcome(rel_path, None, REFUSED_COLLISION, role,
                                       prior_role)

        # 2. emptying — declarations replaced by none (any role, any producer).
        if outcome is None:
            existing = self._read(target)
            if existing is not None:
                old_decls = _count_declarations(existing)
                new_decls = _count_declarations(content)
                if old_decls > 0 and new_decls == 0:
                    outcome = WriteOutcome(
                        rel_path, None, REFUSED_EMPTYING, role, prior_role,
                        detail=f"had {old_decls} declaration(s), new has 0")

        # 3. shrink — a fraction of the repository version (DEV-636).
        if outcome is None:
            base = self.baselines.get(target)
            if base is not None and base.lines >= SHRINK_MIN_BASELINE_LINES:
                new_lines = _line_count(content)
                new_decls = _count_declarations(content)
                small_lines = new_lines < base.lines * SHRINK_REFUSE_RATIO
                small_decls = (base.decls == 0
                               or new_decls < base.decls * SHRINK_REFUSE_RATIO)
                if small_lines and small_decls:
                    outcome = WriteOutcome(
                        rel_path, None, REFUSED_SHRINK, role, prior_role,
                        detail=(f"{new_lines} line(s) / {new_decls} declaration(s) "
                                f"against {base.lines} / {base.decls} in the "
                                f"repository version"))

        digest = sha256_text(content)
        if outcome is None:
            abs_path = artifact_path(self.spec_dir, target)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(content)
            action = ACTION_RENAMED if target != rel_path else ACTION_WRITTEN
            outcome = WriteOutcome(rel_path, target, action, role, prior_role,
                                   sha256=digest)
            if action == ACTION_RENAMED:
                logger.warning(
                    "spec %s: collision at %s (produced by the %s) — the %s's "
                    "version written as %s (policy: rename)", self.spec_id,
                    rel_path, prior_role, role, target)
            self._row(target, kind, role, task_id, digest)
        else:
            logger.warning("spec %s: write refused — %s", self.spec_id,
                           outcome.describe())

        self.entries.append(Entry(
            path=outcome.path or rel_path, role=role, kind=kind.value,
            action=outcome.action, sha256=digest, lines=_line_count(content),
            decls=_count_declarations(content), retry=retry, task_id=task_id,
            design_digest=design_digest_of(self.spec_dir),
            requested=rel_path if outcome.action != ACTION_WRITTEN else None,
            prior_role=prior_role, detail=outcome.detail,
            at=datetime.now(timezone.utc).isoformat()))
        self.outcomes.append(outcome)
        self._save()
        return outcome

    def restore(self, rel_path: str, content: str, *, role: str,
                kind: ArtifactKind = ArtifactKind.CODE,
                task_id: "str | None" = None, retry: int = 0) -> WriteOutcome:
        """Put back bytes the ledger already vouched for (snapshot restore,
        repair rollback). No guards: the content was a landed write."""
        abs_path = artifact_path(self.spec_dir, rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content)
        digest = sha256_text(content)
        self._row(rel_path, kind, role, task_id, digest)
        self.entries.append(Entry(
            path=rel_path, role=role, kind=kind.value, action=ACTION_RESTORED,
            sha256=digest, lines=_line_count(content),
            decls=_count_declarations(content), retry=retry, task_id=task_id,
            design_digest=design_digest_of(self.spec_dir),
            at=datetime.now(timezone.utc).isoformat()))
        outcome = WriteOutcome(rel_path, rel_path, ACTION_RESTORED, role,
                               sha256=digest)
        self.outcomes.append(outcome)
        self._save()
        return outcome

    def note(self, rel_path: str, content: str) -> "Path | None":
        """Write a diagnostic (test output, reports, build logs): traversal-
        protected, no guards, no ledger entry, no artifact row."""
        try:
            abs_path = artifact_path(self.spec_dir, rel_path)
        except ValueError as exc:
            logger.warning("spec %s: diagnostic path %r rejected: %s",
                           self.spec_id, rel_path, exc)
            return None
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content)
        return abs_path

    # ── reads ────────────────────────────────────────────────────────────────

    def _read(self, rel_path: str) -> "str | None":
        try:
            p = artifact_path(self.spec_dir, rel_path)
        except ValueError:
            return None
        if not p.is_file():
            return None
        try:
            return p.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def _row(self, rel_path: str, kind: ArtifactKind, role: str,
             task_id: "str | None", digest: str) -> None:
        if self.db is None:
            return
        try:
            self.db.create_artifact(spec_id=self.spec_id, task_id=task_id,
                                    kind=kind, path=rel_path, sha256=digest,
                                    role=role)
        except TypeError:  # a Database without the role column (tests' fakes)
            self.db.create_artifact(spec_id=self.spec_id, task_id=task_id,
                                    kind=kind, path=rel_path, sha256=digest)

    def hashes(self, paths: Iterable[str]) -> dict[str, str]:
        """sha256 of the bytes ON DISK for each path present (the tested
        manifest's contract: what the check verified, not what was intended)."""
        out: dict[str, str] = {}
        for rel in paths:
            h = self._disk_sha(rel)
            if h is not None:
                out[rel] = h
        return out

    def landed_paths(self, *, roles: "Iterable[str] | None" = None) -> list[str]:
        """Paths whose latest landed entry is still on disk, oldest first."""
        wanted = set(roles) if roles else None
        seen: dict[str, None] = {}
        for e in self.entries:
            if not e.landed:
                continue
            if wanted is not None and e.role not in wanted:
                continue
            if self.producer(e.path) is not None:
                seen[e.path] = None
        return list(seen)

    def recent(self, *, roles: "Iterable[str] | None" = None,
               retry: "int | None" = None,
               actions: "Iterable[str] | None" = None) -> list[Entry]:
        wanted_roles = set(roles) if roles else None
        wanted_actions = set(actions) if actions else None
        out = []
        for e in self.entries:
            if wanted_roles is not None and e.role not in wanted_roles:
                continue
            if retry is not None and e.retry != retry:
                continue
            if wanted_actions is not None and e.action not in wanted_actions:
                continue
            out.append(e)
        return out

    # ── gate text ────────────────────────────────────────────────────────────

    @staticmethod
    def outcomes_block(outcomes: Iterable[WriteOutcome], heading: str) -> str:
        """Markdown for a gate prompt: every rename and refusal, one line each.
        Empty when nothing was renamed or refused."""
        lines = [o.describe() for o in outcomes
                 if o.action in (ACTION_RENAMED, *REFUSALS)]
        if not lines:
            return ""
        return ("\n\n⚠ **" + heading + "** (DEV-642 artifact ledger):\n\n"
                + "\n".join(f"- {ln}" for ln in lines) + "\n")

    def size_block(self, paths: Iterable[str], heading: str) -> str:
        """Per-file line counts against the repository version (DEV-636):
        61 vs 6,130 must read as a red flag on the gate."""
        rows = []
        for rel in paths:
            content = self._read(rel)
            if content is None:
                continue
            base = self.baselines.get(rel)
            new_lines = _line_count(content)
            if base is None:
                rows.append(f"- `{rel}`: {new_lines} line(s) (new file, or "
                            f"repository version never fetched)")
            else:
                flag = ""
                if base.lines and new_lines < base.lines * SHRINK_REFUSE_RATIO:
                    flag = "  ⚠ **far smaller than the repository file**"
                rows.append(f"- `{rel}`: {new_lines} line(s) vs {base.lines} in "
                            f"the repository version{flag}")
        if not rows:
            return ""
        return f"\n\n**{heading}**\n\n" + "\n".join(rows) + "\n"


# ── corpus helpers (used by retry_policy without a Database) ────────────────

def read_entries(root: Path) -> "list[Entry] | None":
    """Entries of the ledger.json under *root* (a live workspace or a
    retry_history snapshot), or None when there is no readable ledger."""
    p = Path(root) / LEDGER_FILE
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return [Entry(**e) for e in data.get("entries", [])]
    except (OSError, ValueError, TypeError):
        return None


ATTEMPT_ROLES = frozenset({"implementer", "synthesizer", "synthesis_repair"})


def attempt_files_from_ledger(root: Path, entries: list[Entry],
                              retry: "int | None") -> "dict[str, str] | None":
    """The files one attempt wrote, read from *root*, selected by the ledger.

    Landed CODE entries by an attempt role, for *retry* when given (a
    snapshot's index) and falling back to every retry when none match — a
    snapshot taken before the ledger existed has entries but no retry stamp
    that agrees. Only files still on disk under *root* count. Returns None
    when the ledger names nothing, so the caller can fall back to a walk.
    """
    def pick(want_retry: "int | None") -> dict[str, str]:
        files: dict[str, str] = {}
        for e in entries:
            if not e.landed or e.role not in ATTEMPT_ROLES:
                continue
            if e.kind != ArtifactKind.CODE.value:
                continue
            if want_retry is not None and e.retry != want_retry:
                continue
            fp = Path(root) / e.path
            if not fp.is_file():
                continue
            try:
                files[e.path] = fp.read_text(errors="replace")
            except OSError:
                continue
        return files

    files = pick(retry)
    if not files and retry is not None:
        files = pick(None)
    return files or None
