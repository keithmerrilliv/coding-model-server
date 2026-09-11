"""Context assembly — one fetch per spec, sections every role selects from (DEV-632).

Phase 3 of the pipeline-kernel refactor. Before it, every role assembled
its own context: the implementer got the files it must modify in DEV-571,
the architect in DEV-599, manifest mode in DEV-604 — three tickets for one
fact ("here are the files this plan will modify, as they exist at
base_ref"). Each plumbing had its own runner round-trip (at least seven per
run, none cached), its own outage handling (the editable fetch parked, the
protected fetch failed soft — DEV-544's blind design) and its own log line,
so each could be starved independently and a fix to one never reached the
others.

``assemble`` is now the only place the runner's read path is called for
prompt context. It runs once per spec — first at plan acceptance, when the
DEV-492 probe already had to read the declared modifications — and the
result is persisted as ``context.json`` beside the ledger, where it
survives every retry wipe. Later roles load it; the runner is asked again
only when the candidate set grew (a manifest named a path the plan did
not), the plan's ``base_ref`` or ``protected_paths`` changed, or a
*symbolic* ref (``main``, ``HEAD``) is older than ``REFRESH_SECONDS`` — a
pinned commit cannot move, so it is never re-read. A refresh that finds
the runner down keeps the last good context and says so, instead of
handing the role nothing (DEV-544); a first fetch that finds the runner
down raises :class:`RunnerOutage`, which the daemon parks on at zero cost
(DEV-620's semantics, now for every role).

The context has three sections:

* **editable** — the declared modification set that reads at base_ref:
  change-surface rows (keyword and backticked), the plan's implement
  outputs, and whatever the caller adds (manifest entries). A candidate
  that reads is a modification whatever the spec called it; one that does
  not is a creation.
* **protected** — ``test_strategy.protected_paths``, read-only. A path in
  both sets is protected: the write path drops it regardless.
* **omitted** — every requested path that did not read, with the runner's
  reason. What a role was NOT shown is recorded, not just what it was.

Roles call :meth:`SpecContext.select` and get a :class:`RoleContext`
with exactly the lists the prompt builders take; the per-role "supplied N
existing file(s) to the architect" line is emitted there, so a journal
still says who saw what. Prior artifacts by role — the reviewer's section
— come from the artifact ledger through :func:`prior_artifacts`; they are
workspace state, not repository state, and need no runner.

Budgeting is pattern 5 (DEV-633), at the bottom of this module.
``allocate`` is the one place the prompt's sections are summed against the
window of the agent that will receive them; ``plan_dispatch`` picks the
agent and the sections together. The per-section knobs in ``executor``
survive as each section's preference — what it gets when the sum fits, and
a ceiling it never exceeds — instead of N independent ceilings whose worst
case was N knobs' worth of prompt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import test_runner
from .models import ArtifactKind

logger = logging.getLogger("orchestrator.context")

CONTEXT_FILE = "context.json"

# A symbolic base_ref (main, HEAD, a branch) can move under a running spec;
# re-verify it against the runner once this old. 0 re-verifies at every role
# boundary (one round-trip per role, as before the stage); a pinned commit
# is never re-verified whatever this says.
REFRESH_SECONDS = int(os.getenv("AUTONOMOUS_CONTEXT_REFRESH_SECONDS", "600"))
# The runner serves at most this many paths per read (its own
# READ_FILES_MAX_PATHS); a longer request is split so nothing is silently
# dropped past the cap.
FETCH_CHUNK = max(1, int(os.getenv("AUTONOMOUS_CONTEXT_FETCH_CHUNK", "40")))

SECTION_EDITABLE = "editable"
SECTION_PROTECTED = "protected"

_PINNED_REF = re.compile(r"^[0-9a-f]{7,40}$")

FetchFn = Callable[..., tuple[list[tuple[str, str]], list[str]]]


class RunnerOutage(RuntimeError):
    """The runner did not answer the context fetch at all (transport-level,
    DEV-620) while paths were pending and no earlier context exists. Raised
    BEFORE any model call, so the catcher can park the task at zero cost
    instead of letting a role work blind — run 19's blind rewrite, run 9's
    blind design (DEV-544)."""


# ── candidate derivation ─────────────────────────────────────────────────────

# A change-surface row whose second column marks the path as modified.
_CHANGE_SURFACE_ROW = re.compile(
    r"^\|\s*`?([^`|]+?)`?\s*\|\s*(?:\*\*)?(modif\w*)\b",
    re.IGNORECASE | re.MULTILINE,
)
# DEV-621: run 19's hand-written table used a descriptive second column
# ("What changes here"), so every row failed the keyword match above and
# the existing-file fetch lost the table entirely. Backticked-path rows
# are a WEAKER tier of declaration: they become fetch candidates
# (existence at base_ref is the real test), but they never carry the
# DEV-492 hard-stop, which stays keyword-only so greenfield tables keep
# planning.
_TABLE_PATH_ROW = re.compile(r"^\|\s*`([^`|]+?)`\s*\|", re.MULTILINE)


def declared_modifications(spec_md: str) -> list[str]:
    """Paths a spec's change-surface table marks as modified, not created."""
    if not spec_md:
        return []
    return [
        m.group(1).strip()
        for m in _CHANGE_SURFACE_ROW.finditer(spec_md)
        if m.group(1).strip() and m.group(1).strip().lower() != "path"
    ]


def change_surface_path_rows(spec_md: str) -> list[str]:
    """Backticked first-column paths of any table row (DEV-621)."""
    if not spec_md:
        return []
    seen: dict[str, None] = {}
    for m in _TABLE_PATH_ROW.finditer(spec_md):
        p = m.group(1).strip()
        if p and p.lower() != "path" and ("/" in p or "." in p):
            seen.setdefault(p)
    return list(seen)


def planned_outputs(plan: dict) -> list[str]:
    """File paths the plan's implement phase says it will write (DEV-571).

    The single-call path's candidates, from the plan the operator approved
    rather than from prose parsing. Paths that do not exist in the repo are
    simply new files — the fetch's own not-found branch sorts them — so a
    wrong guess costs nothing.
    """
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(phases, list):
        return []
    out: list[str] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        if str(phase.get("name", "")).strip().lower() != "implement":
            continue
        for path in phase.get("outputs") or []:
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
    return list(dict.fromkeys(out))


def _strategy(plan: dict) -> dict:
    strategy = plan.get("test_strategy") if isinstance(plan, dict) else None
    return strategy if isinstance(strategy, dict) else {}


def protected_paths(plan: dict) -> list[str]:
    raw = _strategy(plan).get("protected_paths") or []
    return list(dict.fromkeys(str(p).strip() for p in raw if p and str(p).strip()))


# ── the context ──────────────────────────────────────────────────────────────

@dataclass
class Omission:
    """A requested path that was not read, and why."""
    path: str
    section: str
    reason: str


@dataclass
class RoleContext:
    """What one role selects from the spec context — the prompt builders'
    argument shapes, nothing more."""
    role: str
    existing_files: list[tuple[str, str]]     # editable, in candidate order
    reference_files: list[tuple[str, str]]    # protected, read-only
    new_files: list[str]                      # planned outputs that did not read
    omitted: list[Omission]
    stale: bool = False                       # a refresh failed; this is the last good fetch

    @property
    def existing_by_path(self) -> dict[str, str]:
        return dict(self.existing_files)


@dataclass
class SpecContext:
    """One spec's repository context at base_ref, as persisted in context.json."""
    spec_id: str
    repo: Optional[str]
    base_ref: str
    candidates: list[str]           # editable candidates requested, in order
    declared: list[str]             # keyword-declared modifications (DEV-492 tier)
    protected_paths: list[str]
    editable: dict[str, str] = field(default_factory=dict)
    protected: dict[str, str] = field(default_factory=dict)
    omitted: list[Omission] = field(default_factory=list)
    fetched_at: float = 0.0
    fetched_by: str = ""            # the role whose call ran the fetch
    fetches: int = 0                # runner round-trips this context has cost
    stale: bool = False             # runtime only: a refresh found the runner down

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def empty(cls, spec_id: str, *, repo: Optional[str] = None,
              base_ref: str = "HEAD") -> "SpecContext":
        return cls(spec_id=spec_id, repo=repo, base_ref=base_ref,
                   candidates=[], declared=[], protected_paths=[])

    @classmethod
    def from_files(cls, spec_id: str,
                   editable: Iterable[tuple[str, str]] = (),
                   protected: Iterable[tuple[str, str]] = (),
                   *, declared: Iterable[str] = ()) -> "SpecContext":
        """A context built from in-memory files — for tests and callers that
        already hold the content (no runner involved)."""
        editable = list(editable)
        protected = list(protected)
        return cls(spec_id=spec_id, repo=None, base_ref="HEAD",
                   candidates=[p for p, _ in editable], declared=list(declared),
                   protected_paths=[p for p, _ in protected],
                   editable=dict(editable), protected=dict(protected),
                   fetched_at=time.time(), fetched_by="caller", fetches=0)

    # ── sections ────────────────────────────────────────────────────────────

    @property
    def editable_files(self) -> list[tuple[str, str]]:
        return list(self.editable.items())

    @property
    def protected_files(self) -> list[tuple[str, str]]:
        return list(self.protected.items())

    def existing(self, path: str) -> Optional[str]:
        """Repository content of *path* at base_ref, or None when it does not
        exist there (a creation) or was never a candidate."""
        return self.editable.get(path)

    def new_files(self, planned: Iterable[str]) -> list[str]:
        """Planned paths that did not read at base_ref — the ones a prompt
        must mark EMIT WHOLE (DEV-638)."""
        return [p for p in dict.fromkeys(planned)
                if p not in self.editable and p not in self.protected]

    def select(self, role: str, *, planned: Iterable[str] = ()) -> RoleContext:
        """The role's view, with the journal lines that say what it saw."""
        existing = self.editable_files
        reference = self.protected_files
        if existing:
            logger.info("spec %s: supplied %d existing file(s) to the %s: %s",
                        self.spec_id, len(existing), role,
                        ", ".join(p for p, _ in existing))
        elif self.declared:
            logger.warning("spec %s: %d file(s) marked modify but none could be "
                           "read — %s is working blind", self.spec_id,
                           len(self.declared), role)
        if reference:
            logger.info("spec %s: supplied %d protected file(s) as read-only "
                        "context to the %s: %s", self.spec_id, len(reference),
                        role, ", ".join(p for p, _ in reference))
        if self.stale:
            logger.warning("spec %s: the %s is working from the last good "
                           "context fetch (%s) — the runner did not answer "
                           "the refresh", self.spec_id, role,
                           _stamp(self.fetched_at))
        return RoleContext(role=role, existing_files=existing,
                           reference_files=reference,
                           new_files=self.new_files(planned),
                           omitted=list(self.omitted), stale=self.stale)

    # ── reuse policy ────────────────────────────────────────────────────────

    def covers(self, candidates: Iterable[str], protected: Iterable[str]) -> bool:
        return (set(candidates) <= set(self.candidates)
                and set(protected) <= set(self.protected_paths))

    def fresh(self, now: float) -> bool:
        if _PINNED_REF.match(self.base_ref or ""):
            return True
        return (now - self.fetched_at) < REFRESH_SECONDS

    # ── persistence ─────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Event payload: what was fetched and what was omitted, no content."""
        return {
            "repo": self.repo,
            "base_ref": self.base_ref,
            "fetched_by": self.fetched_by,
            "fetches": self.fetches,
            "editable": list(self.editable),
            "protected": list(self.protected),
            "omitted": [f"{o.path} ({o.section}): {o.reason[:120]}"
                        for o in self.omitted],
            "editable_chars": sum(len(c) for c in self.editable.values()),
            "protected_chars": sum(len(c) for c in self.protected.values()),
        }

    def save(self, spec_dir: Path) -> Path:
        path = spec_dir / CONTEXT_FILE
        data = asdict(self)
        data.pop("stale", None)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, spec_dir: Path) -> "SpecContext | None":
        path = spec_dir / CONTEXT_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
            data["omitted"] = [Omission(**o) for o in data.get("omitted") or []]
            data.pop("stale", None)
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("context.json unreadable at %s (%s) — will re-fetch",
                           path, exc)
            return None


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else "never"


# ── assembly ─────────────────────────────────────────────────────────────────

def _fetch_all(fetch: FetchFn, repo: str, paths: list[str], base_ref: str,
               ) -> tuple[list[tuple[str, str]], list[str]]:
    """Every path, in runner-sized chunks. Raises RunnerOutage on a
    transport-class answer (one unprefixed problem, no files)."""
    files: list[tuple[str, str]] = []
    problems: list[str] = []
    for i in range(0, len(paths), FETCH_CHUNK):
        chunk = paths[i:i + FETCH_CHUNK]
        got, bad = fetch(repo, chunk, base_ref)
        if not got and test_runner.problems_indicate_runner_outage(bad, chunk):
            raise RunnerOutage(bad[0])
        files.extend(got)
        problems.extend(bad)
    return files, problems


def _reason_for(path: str, problems: list[str]) -> str:
    prefix = f"{path}:"
    for p in problems:
        if p.startswith(prefix):
            return p[len(prefix):].strip()
    return "not returned by the runner"


def assemble(
    *,
    spec_id: str,
    spec_dir: Optional[Path],
    plan: dict,
    spec_md: str,
    role: str,
    extra_candidates: Iterable[str] = (),
    fetch: Optional[FetchFn] = None,
    now: Optional[float] = None,
    force: bool = False,
) -> tuple[SpecContext, bool]:
    """The spec's context, fetched or reused.

    *spec_dir* None means no persistence (a probe without a workspace).
    Returns ``(context, fetched)``: *fetched* is True when the runner was
    asked on this call — the caller records baselines and the event then,
    and only then. Raises :class:`RunnerOutage` when the runner is down and
    there is no earlier context to fall back on; every other read failure
    degrades soft with a warning naming the role, as the fetches always
    did.
    """
    fetch = fetch or test_runner.fetch_repo_files
    now = time.time() if now is None else now
    strategy = _strategy(plan)
    repo = strategy.get("repo")
    base_ref = strategy.get("base_ref") or "HEAD"
    declared = declared_modifications(spec_md)
    candidates = list(dict.fromkeys([
        *declared, *change_surface_path_rows(spec_md),
        *planned_outputs(plan), *extra_candidates]))
    protected = protected_paths(plan)

    if not repo:
        # No registered repo means no runner-side checkout to read from —
        # the local-framework case (pytest/node), where the spec dir is the
        # whole world and there is nothing to fetch.
        return SpecContext(spec_id=spec_id, repo=None, base_ref=base_ref,
                           candidates=candidates, declared=declared,
                           protected_paths=protected, fetched_at=now,
                           fetched_by=role), False

    stored = SpecContext.load(spec_dir) if spec_dir is not None else None
    reusable = (stored is not None and not force
                and stored.repo == repo and stored.base_ref == base_ref
                and stored.covers(candidates, protected))
    if reusable and stored is not None and stored.fresh(now):
        logger.info("spec %s: context reused for the %s (%d editable, %d "
                    "protected, fetched %s by the %s)", spec_id, role,
                    len(stored.editable), len(stored.protected),
                    _stamp(stored.fetched_at), stored.fetched_by)
        return stored, False

    # Protected wins when a path is in both sets: the write path drops it
    # whatever any role produces, so showing it as editable would invite an
    # edit that is discarded.
    editable_wanted = [p for p in candidates if p not in set(protected)]
    paths = list(dict.fromkeys([*editable_wanted, *protected]))
    if not paths:
        ctx = SpecContext(spec_id=spec_id, repo=repo, base_ref=base_ref,
                          candidates=candidates, declared=declared,
                          protected_paths=protected, fetched_at=now,
                          fetched_by=role)
        return ctx, False

    try:
        files, problems = _fetch_all(fetch, repo, paths, base_ref)
    except RunnerOutage as exc:
        if reusable and stored is not None:
            # DEV-544: a transient outage on a refresh must not strip the
            # context a role already had. Keep the last good fetch and say so.
            logger.warning("spec %s: context refresh for the %s found the "
                           "runner down (%s) — keeping the fetch from %s",
                           spec_id, role, exc, _stamp(stored.fetched_at))
            stored.stale = True
            return stored, False
        raise
    except Exception as exc:  # a daemon-side failure, not a runner answer
        logger.warning("spec %s: existing-file read failed (%s); the %s will "
                       "not see the files it must modify", spec_id, exc, role)
        if reusable and stored is not None:
            stored.stale = True
            return stored, False
        return SpecContext(spec_id=spec_id, repo=repo, base_ref=base_ref,
                           candidates=candidates, declared=declared,
                           protected_paths=protected, fetched_at=now,
                           fetched_by=role), False

    got = dict(files)
    protected_set = set(protected)
    ctx = SpecContext(
        spec_id=spec_id, repo=repo, base_ref=base_ref,
        candidates=candidates, declared=declared, protected_paths=protected,
        editable={p: got[p] for p in editable_wanted if p in got},
        protected={p: got[p] for p in protected if p in got},
        fetched_at=now, fetched_by=role,
        fetches=(stored.fetches if stored is not None else 0) + 1,
    )
    for p in paths:
        if p in got:
            continue
        section = SECTION_PROTECTED if p in protected_set else SECTION_EDITABLE
        reason = _reason_for(p, problems)
        ctx.omitted.append(Omission(p, section, reason))
        # DEV-620: log every problem — a swallowed one was run 19's only
        # trace. A creation's "not found" costs one benign line.
        logger.warning("spec %s: %s-file read problem — %s: %s", spec_id,
                       section, p, reason)

    if stored is not None and stored.repo == repo and stored.base_ref == base_ref:
        before = {**stored.protected, **stored.editable}
        changed = sorted(p for p in set(ctx.editable) | set(ctx.protected)
                         if p in before and before[p] != got[p])
        appeared = sorted((set(ctx.editable) | set(ctx.protected))
                          - set(stored.editable) - set(stored.protected))
        logger.info("spec %s: context refreshed at %s for the %s — %d changed"
                    "%s, %d new%s (fetch %d)", spec_id, base_ref, role,
                    len(changed), f" ({', '.join(changed)})" if changed else "",
                    len(appeared), f" ({', '.join(appeared)})" if appeared else "",
                    ctx.fetches)
    else:
        logger.info("spec %s: context assembled at %s for the %s — %d editable, "
                    "%d protected, %d omitted", spec_id, base_ref, role,
                    len(ctx.editable), len(ctx.protected), len(ctx.omitted))
    try:
        if spec_dir is not None:
            ctx.save(spec_dir)
    except OSError as exc:
        logger.warning("spec %s: could not persist context.json (%s) — the "
                       "next role will fetch again", spec_id, exc)
    return ctx, True


# ── budgeting: one sum against the destination window (DEV-633) ──────────────
#
# Before this, each prompt section clamped itself at its own char knob and
# nothing added them up. The worst case was N knobs' worth of prompt, and an
# operator tuning one knob could not see the others: run 20's
# AUTONOMOUS_EXISTING_FILES_MAX_CHARS override silently raised the protected
# ceiling 7.5x because the two sections shared the constant, and run 21's
# implementer prompt went past 1 MB into a 413 (DEV-627). The fit check that
# was supposed to catch that (DEV-624) ran AFTER the prompt was rendered and
# could only move the prompt to a bigger agent, never make it smaller.
#
# ``allocate`` is the single sum. It is told what the prompt costs before any
# file section (``fixed_chars``), what the destination window holds, and what
# the completion needs; it hands each section, in priority order, the smaller
# of its knob and what is left. A section that runs out is trimmed rather than
# the prompt overflowing, and every trimmed path is reported so the render can
# still name it — "you were not shown X" is actionable, a missing section is
# not. The knobs stay: they are the preference each section gets WHEN THE SUM
# FITS, and a ceiling it never exceeds. They are no longer independent.

# Conservative for code-heavy prompts: real tokenizers average ~3.3-3.8
# chars/token on this repo's source, so dividing by 3 overestimates the
# prompt — the safe direction for a fit check.
CHARS_PER_TOKEN = max(1, int(os.getenv("AUTONOMOUS_PROMPT_CHARS_PER_TOKEN", "3")))
# Fraction of the window the input may claim once the completion is reserved.
# The chars/token estimate is a guess; this is the margin for it being wrong
# in the unsafe direction on a prompt that is mostly prose rather than code.
HEADROOM = float(os.getenv("AUTONOMOUS_PROMPT_HEADROOM", "0.95"))


class PromptTooLarge(RuntimeError):
    """No available window holds this prompt even with every droppable
    section dropped — the fixed part (spec, design, instructions) plus the
    completion reserve is already over. Raised BEFORE the call, so the
    catcher parks it as a no-verdict instead of the model server discovering
    it as a 413 (DEV-624's terminal failure mode)."""


@dataclass(frozen=True)
class Section:
    """One droppable block of files, with the knob it prefers."""
    name: str
    files: list[tuple[str, str]]
    preferred_max_chars: Optional[int] = None

    @property
    def wanted_chars(self) -> int:
        return sum(len(c) for _, c in self.files)


@dataclass
class SectionBudget:
    """What one section actually got."""
    name: str
    kept: list[tuple[str, str]]
    dropped: list[str]
    budget: int                     # chars the allocator gave it
    preferred: Optional[int]        # its knob, for the log line

    @property
    def chars(self) -> int:
        return sum(len(c) for _, c in self.kept)

    @property
    def squeezed(self) -> bool:
        """Trimmed by the aggregate sum rather than by its own knob."""
        return bool(self.dropped) and (self.preferred is None
                                       or self.budget < self.preferred)


@dataclass
class Allocation:
    """The whole prompt's budget, as one decision."""
    agent: Optional[str]
    window_tokens: Optional[int]
    completion_tokens: int
    fixed_chars: int
    input_budget_chars: int         # chars the file sections may share
    sections: dict[str, SectionBudget] = field(default_factory=dict)

    def files(self, name: str) -> list[tuple[str, str]]:
        sec = self.sections.get(name)
        return list(sec.kept) if sec else []

    def dropped(self, name: str) -> list[str]:
        sec = self.sections.get(name)
        return list(sec.dropped) if sec else []

    @property
    def section_chars(self) -> int:
        return sum(s.chars for s in self.sections.values())

    @property
    def prompt_chars(self) -> int:
        return self.fixed_chars + self.section_chars

    @property
    def prompt_tokens(self) -> int:
        return self.prompt_chars // CHARS_PER_TOKEN

    @property
    def needed_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def dropped_any(self) -> bool:
        return any(s.dropped for s in self.sections.values())

    @property
    def fits(self) -> bool:
        return self.window_tokens is None or self.needed_tokens <= self.window_tokens

    def describe(self) -> str:
        parts = [f"{s.name} {s.chars}/{s.budget}"
                 + (f" (-{len(s.dropped)})" if s.dropped else "")
                 for s in self.sections.values()]
        return (f"fixed {self.fixed_chars} + " + ", ".join(parts)
                + f" = ~{self.prompt_tokens} tok + {self.completion_tokens} "
                + f"completion vs {self.window_tokens or '?'}")


def _fill(section: Section, budget: int) -> SectionBudget:
    """Greedy, in candidate order: a file that does not fit is skipped and
    named, and a later smaller one may still fit. Order is priority — the
    caller sorts before it gets here — so skipping rather than stopping only
    ever adds content."""
    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    left = max(0, budget)
    for path, content in section.files:
        if len(content) > left:
            dropped.append(path)
            continue
        left -= len(content)
        kept.append((path, content))
    return SectionBudget(name=section.name, kept=kept, dropped=dropped,
                         budget=max(0, budget),
                         preferred=section.preferred_max_chars)


def allocate(sections: Iterable[Section], *, fixed_chars: int,
             window_tokens: Optional[int], completion_tokens: int,
             agent: Optional[str] = None) -> Allocation:
    """Give each section, in priority order, the smaller of its knob and what
    the destination window has left.

    *sections* are in priority order: the declared modification set first
    (a role that cannot see what it must edit invents it), protected
    references next (they prevent redeclaration), prior artifacts last (they
    are recoverable from the ledger). *fixed_chars* is everything else the
    prompt costs — spec, design, notes, instructions — measured by building
    the message with the file sections empty.

    An unknown window (``window_tokens`` None: an agent not in the config)
    applies no aggregate pressure and every section gets its knob, which is
    exactly the pre-DEV-633 behaviour.
    """
    sections = list(sections)
    if window_tokens is None:
        budget_chars = -1  # unbounded
    else:
        spare = int(window_tokens * HEADROOM) - completion_tokens
        budget_chars = max(0, spare * CHARS_PER_TOKEN - fixed_chars)
    alloc = Allocation(agent=agent, window_tokens=window_tokens,
                       completion_tokens=completion_tokens,
                       fixed_chars=fixed_chars,
                       input_budget_chars=budget_chars)
    left = budget_chars
    for section in sections:
        knob = section.preferred_max_chars
        if budget_chars < 0:
            share = knob if knob is not None else section.wanted_chars
        else:
            share = left if knob is None else min(knob, left)
        got = _fill(section, share)
        alloc.sections[section.name] = got
        if budget_chars >= 0:
            left = max(0, left - got.chars)
    return alloc


def plan_dispatch(spec_id: str, *, role: str, sections: Iterable[Section],
                  fixed_chars: int, completion_tokens: int,
                  agent: Optional[str], candidates: Iterable[str] = (),
                  window_of: Callable[[Optional[str]], Optional[int]],
                  reserve_of: Optional[Callable[[Optional[str]], int]] = None,
                  ) -> Allocation:
    """Pick the agent AND the sections together, then say what it cost.

    The chosen agent goes first: the architect's complexity recommendation
    and the retry rotation picked it for a reason, and a prompt that fits it
    should not be moved. When it cannot hold every section's preference, the
    candidates are tried in order and the first that can holds the whole
    prompt uncut — escalating to a bigger window is always better than
    dropping context (DEV-624). Only when no window can is anything dropped,
    from the largest window available, lowest priority first.

    ``window_of`` and ``reserve_of`` are injected — this module knows
    nothing about the agent config — and return the candidate's context
    window and any reasoning budget its server spends ahead of the
    completion.

    Raises :class:`PromptTooLarge` when even that window cannot hold the
    fixed part plus the completion reserve. Nothing has been spent at that
    point, so the caller parks rather than discovering it as a 413.
    """
    sections = list(sections)
    chain: list[Optional[str]] = [agent]
    chain += [c for c in candidates if c != agent]
    best: Optional[Allocation] = None
    for cand in chain:
        window = window_of(cand)
        # The reasoning budget is spent in the same window as the completion
        # and before it (DEV-616), so it is reserved the same way.
        reserved = completion_tokens + (reserve_of(cand) if reserve_of else 0)
        alloc = allocate(sections, fixed_chars=fixed_chars,
                         window_tokens=window, completion_tokens=reserved,
                         agent=cand)
        # An escalation candidate must have a KNOWN window: an agent missing
        # from the config estimates as unbounded, which would make it the
        # winner of every oversized prompt for the worst possible reason. The
        # originally chosen agent is exempt — an unknown pick is left alone,
        # exactly as it was before any of this existed.
        known = window is not None or cand == agent
        if known and not alloc.dropped_any and alloc.fits:
            if cand != agent:
                logger.warning(
                    "spec %s: the %s prompt does not fit %r — dispatching to "
                    "%r (%s) instead (DEV-633: %s)", spec_id, role, agent,
                    cand, window, alloc.describe())
            # Say the sum on EVERY dispatch, not only when it had to move or
            # cut something. Run 26 dispatched five implementer prompts and
            # the journal recorded nothing about any of them, so whether the
            # budget had run at all could only be established by replaying it
            # afterwards. A guard that is silent when it passes is a guard
            # nobody can show was armed (DEV-620's lesson).
            logger.info("spec %s: %s prompt budget — %s",
                        spec_id, role, alloc.describe())
            return alloc
        if window is not None and (best is None
                                   or window > (best.window_tokens or 0)):
            best = alloc
        elif best is None:
            best = alloc
    assert best is not None  # chain is never empty
    if not best.fits:
        raise PromptTooLarge(
            f"the {role} prompt needs ~{best.needed_tokens} tokens with every "
            f"droppable section dropped and the largest window holds "
            f"{best.window_tokens} — {best.describe()}")
    for sec in best.sections.values():
        if not sec.dropped:
            continue
        # Say WHICH ceiling bit: an operator can raise a knob, but no knob
        # buys room the window does not have.
        why = (f"no window holds the whole {role} prompt"
               if sec.squeezed else
               f"the {sec.name} section is at its own {sec.preferred}-char knob")
        logger.warning("spec %s: %s — %d %s file(s) not shown to %r: %s",
                       spec_id, why, len(sec.dropped), sec.name, best.agent,
                       ", ".join(sec.dropped))
    logger.info("spec %s: %s prompt budget — %s", spec_id, role, best.describe())
    return best

# ── prior artifacts (workspace state, no runner) ─────────────────────────────

def prior_artifacts(db: Any, spec_id: str, spec_dir: Path, *,
                    roles: Optional[Iterable[str]] = None,
                    ) -> list[tuple[str, str]]:
    """Code artifacts on disk as (path, content), latest row per path.

    The reviewer's section. Artifact rows accumulate across retries (the
    retry wipe deletes files, not rows), so each path can have N+1 rows
    after N retries — reading all of them duplicated every file N+1 times
    in the reviewer prompt (DEV-143). Rows are created_at-ordered; the
    latest per path wins. Binary deliverables get a placeholder so the path
    still appears in the prompt without a UTF-8 crash.
    """
    wanted = set(roles) if roles else None
    latest_by_path: dict[str, Any] = {}
    for art in db.list_artifacts(spec_id, kind=ArtifactKind.CODE):
        if wanted is not None and getattr(art, "role", None) not in wanted:
            continue
        latest_by_path[art.path] = art
    out: list[tuple[str, str]] = []
    for art in latest_by_path.values():
        fpath = spec_dir / art.path
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text()
        except UnicodeDecodeError:
            content = (f"[binary file, {fpath.stat().st_size} bytes — "
                       f"reviewer cannot inspect content]")
        out.append((art.path, content))
    return out
