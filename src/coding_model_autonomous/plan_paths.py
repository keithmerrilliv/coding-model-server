"""Resolve the plan's phase paths against the target repository — DEV-601.

`phases[*].inputs` and `phases[*].outputs` are free text produced by a model,
and until this module nothing checked them against the repo the plan names. A
bare filename where the repo holds `Dir/File.swift` is not a harmless typo: on
run 42 one produced five distinct failures from a single cause.

1. The architect's context served 0 editable files — the paths did not resolve,
   so nothing was fetched.
2. The implementer's context served 0 editable files, with no manifest to heal
   it (`use_manifest_mode` was False at 5 files against a threshold of 8).
3. With no existing content, DEV-604 makes whole-file emission "correct", so
   three existing files were regenerated from a design that described only a
   cursor addition.
4. The planned-output check compares the plan's strings to the produced paths,
   so it reported "not produced" for files that were produced, forever.
5. Its feedback then told the implementer each file "is a NEW file. Emit it
   whole" — at a root-level path outside the project's synchronized groups,
   where nothing is ever compiled.

`planned_outputs()` states the assumption that permits all five: *"Paths that do
not exist in the repo are simply new files ... so a wrong guess costs nothing."*
A wrong guess cost four attempts and came within one un-rejected gate of
destroying working code.

DESIGN. Everything here is pure: callers inject an `exists` predicate, so the
whole module is testable without a runner, and the fetch it depends on is the
one the plan probe already performs.

A path is classified as exactly one of:

* ``resolved``    — reads at base_ref; it is a modification, nothing to do.
* ``corrected``   — does not read, but its basename reads uniquely under a
  directory the plan already knows about. That is a path error, not a new file,
  and the plan is rewritten to the real path.
* ``placeholder`` — a literal like ``<source files>``. Never a path.
* ``new``         — does not read and nothing suggests otherwise. Genuinely a
  file to create, which is legitimate and left alone.

The ordering matters: ``corrected`` must be tried before ``new``, because
"does not read" is exactly what both look like and only one of them is safe.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

# `<source files>`, `<new test files>`, `TBD`, `...` — a model's way of saying
# "something goes here". Run 17 carried `<source files>` as phases[test].inputs.
_PLACEHOLDER_RE = re.compile(r"^\s*(<[^>]*>|\.{3}|tbd|n/?a|none)\s*$", re.IGNORECASE)

PHASE_PATH_KEYS = ("inputs", "outputs")

# Inputs that name an artifact of the run rather than a repository file. These
# are correct as bare names and must never be "corrected" into the repo.
_RUN_ARTIFACTS = frozenset({
    "spec.md", "design.md", "plan.yaml", "manifest.md", "test_report.md",
    "complexity.json", "context.json", "ledger.json", "delivery_report.md",
})


def is_placeholder(path: str) -> bool:
    """True for a literal that was never a path (DEV-601, run 17)."""
    return bool(_PLACEHOLDER_RE.match(path or ""))


def is_run_artifact(path: str) -> bool:
    """True for a spec-workspace artifact — correct as a bare name."""
    return (path or "").strip() in _RUN_ARTIFACTS


# DEV-733. A path the repository cannot confirm is not always a new file: it is
# also what a typo in a new file's NAME looks like, and no repo lookup can tell
# the two apart. Run 43's planner wrote `AudoscapeStateTests.swift` for a spec
# whose change-surface table said `AudioscapeStateTests.swift` — one missing
# letter. The resolver correctly called it new, because a file that does not
# exist yet resolves under no directory and the correction ladder had nothing
# to try. The spec had the answer the whole time, in a table already parsed.
#
# The bar is deliberately tight, because the cost of a WRONG correction is
# higher than the cost of a missed one: a missed typo is caught at the plan
# gate, while a wrong correction silently retargets a file nobody asked for.
# Two edits catches transcription slips (a dropped letter, a transposition) and
# refuses `FooTests.swift` -> `FooBarTests.swift`, which is three.
TYPO_MAX_DISTANCE = 2
# Below this, two edits is most of the name: `a.py` and `b.py` are one edit
# apart and have nothing to do with each other.
TYPO_MIN_BASENAME = 8


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance between *a* and *b*, abandoned past *limit*.

    Returns ``limit + 1`` rather than the true distance once it is exceeded —
    callers only ever ask "is this within the bar?", and the early exit keeps
    a long pair from being walked in full.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (0 if ca == cb else 1)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


# A change-surface table's first column is backticked prose as often as it is a
# path. Measuring this guard against the spec archive turned up
# `grep -c 'if Task.isCancelled { break }' ElectricSheep/HallucinationEngine.swift`
# sitting in one — its basename is a real filename, so without this it becomes a
# correction target and rewrites a good path into a shell command. A repository
# path in any spec we have ever run carries none of these.
_NOT_A_PATH = re.compile(r"""[\s'"`|&;$()<>*?\\]""")


_QUOTES = "`'\""


def unquote_path(path: str) -> str:
    """*path* without surrounding backticks/quotes and trailing punctuation
    (DEV-786). A bare path comes back unchanged."""
    p = (path or "").strip()
    while len(p) >= 2 and p[0] in _QUOTES and p[-1] == p[0]:
        p = p[1:-1].strip()
    return p.rstrip(",;:").strip()


def is_plausible_path(candidate: str) -> bool:
    """False for a change-surface cell that is prose or a command, not a path."""
    c = (candidate or "").strip()
    return bool(c) and not _NOT_A_PATH.search(c)


def declared_near_misses(path: str, declared: Iterable[str],
                         exclude: Iterable[str] = ()) -> list[str]:
    """Declared paths whose basename is a typo's distance from *path*'s.

    *declared* is the spec's change surface — paths the operator wrote and the
    pipeline already parses. *exclude* is the plan's own path set: a declared
    path the plan ALREADY carries verbatim is not a correction target, because
    then the plan names both spellings and collapsing them would silently drop
    a file the plan asked for. That is a judgement this module does not make.

    Basenames are compared, not whole paths, so this also catches the new file
    written to the wrong directory — run 42's root-level path outside the
    project's synchronized groups, where nothing is ever compiled.
    """
    base = posixpath.basename((path or "").strip())
    if len(base) < TYPO_MIN_BASENAME:
        return []
    _, ext = posixpath.splitext(base)
    skip = {p for p in exclude}
    out: list[str] = []
    for cand in declared:
        cand = (cand or "").strip()
        if not cand or cand == path or cand in skip or cand in out:
            continue
        if not is_plausible_path(cand):
            continue
        cand_base = posixpath.basename(cand)
        if posixpath.splitext(cand_base)[1] != ext:
            continue
        if _edit_distance(base, cand_base, TYPO_MAX_DISTANCE) <= TYPO_MAX_DISTANCE:
            out.append(cand)
    return out


def phase_paths(plan: dict) -> list[tuple[str, str, int, str]]:
    """Every (phase_name, key, index, path) in phases[*].inputs/outputs.

    Order is stable so a caller can rewrite in place.
    """
    out: list[tuple[str, str, int, str]] = []
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(phases, list):
        return out
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        name = str(phase.get("name", "")).strip()
        for key in PHASE_PATH_KEYS:
            values = phase.get(key)
            if not isinstance(values, list):
                continue
            for i, value in enumerate(values):
                if isinstance(value, str) and value.strip():
                    out.append((name, key, i, value.strip()))
    return out


def known_directories(paths: Iterable[str]) -> list[str]:
    """Distinct parent directories of *paths*, nearest-root first.

    These are the directories the plan already demonstrates are real — in
    practice `test_strategy.protected_paths`, which the operator writes with
    full paths, and any phase path that resolved. On run 42 all six protected
    files sat under `ElectricSheep/`, which is precisely the signal that was
    available and unused.
    """
    dirs: list[str] = []
    for p in paths:
        d = posixpath.dirname((p or "").strip())
        if d and d not in dirs:
            dirs.append(d)
    return sorted(dirs, key=lambda d: (d.count("/"), d))


def correction_candidates(path: str, directories: Iterable[str]) -> list[str]:
    """`<dir>/<basename>` for each known directory, excluding *path* itself."""
    base = posixpath.basename((path or "").strip())
    if not base:
        return []
    out: list[str] = []
    for d in directories:
        cand = posixpath.join(d, base)
        if cand != path and cand not in out:
            out.append(cand)
    return out


@dataclass
class PathResolution:
    """What resolving one plan path concluded."""
    path: str
    status: str                      # resolved | corrected | placeholder | new
    corrected_to: str | None = None
    ambiguous: list[str] = field(default_factory=list)
    # Which anchor decided it: "" for none, "directory" for a basename that
    # reads under a known directory, "declared" for a near-miss of the spec's
    # change surface (DEV-733). The two mean different things to a reader —
    # one says the file exists elsewhere, the other says the NAME is wrong —
    # so the journal should not have to guess which fired.
    source: str = ""

    @property
    def is_problem(self) -> bool:
        return self.status == "placeholder" or bool(self.ambiguous)


@dataclass
class PlanPathReport:
    resolutions: list[PathResolution] = field(default_factory=list)

    @property
    def corrections(self) -> dict[str, str]:
        """{wrong path: real path}, deduped — what to rewrite in the plan."""
        return {r.path: r.corrected_to for r in self.resolutions
                if r.status == "corrected" and r.corrected_to}

    @property
    def placeholders(self) -> list[str]:
        return sorted({r.path for r in self.resolutions
                       if r.status == "placeholder"})

    @property
    def ambiguous(self) -> list[PathResolution]:
        return [r for r in self.resolutions if r.ambiguous]

    @property
    def new_paths(self) -> list[str]:
        return sorted({r.path for r in self.resolutions if r.status == "new"})

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for r in self.resolutions:
            counts[r.status] = counts.get(r.status, 0) + 1
        return ", ".join(f"{k}={counts[k]}" for k in sorted(counts)) or "none"


def resolve_plan_paths(plan: dict, exists: Callable[[str], bool],
                       extra_directories: Iterable[str] = (),
                       declared_paths: Iterable[str] = ()) -> PlanPathReport:
    """Classify every phase path. *exists* answers "reads at base_ref".

    *extra_directories* seeds the correction search with directories known to
    be real from outside the phases — `test_strategy.protected_paths` is the
    intended source, because the operator writes those with full paths.

    A basename that resolves under exactly one known directory is CORRECTED. One
    that resolves under several is AMBIGUOUS and reported rather than guessed:
    picking between `Foo/Util.swift` and `Bar/Util.swift` is not this module's
    decision to make.
    """
    report = PlanPathReport()
    entries = phase_paths(plan)

    resolved_paths = [p for _, _, _, p in entries if exists(p)]
    directories = known_directories(list(extra_directories) + resolved_paths)
    # The plan's own paths, so a declared path the plan already carries is
    # never proposed as a correction for a different one (DEV-733).
    plan_own = {p for _, _, _, p in entries}
    declared = list(declared_paths)

    seen: dict[str, PathResolution] = {}
    def _classify(p: str) -> PathResolution:
        if is_placeholder(p):
            res = PathResolution(p, "placeholder")
        elif is_run_artifact(p) or exists(p):
            res = PathResolution(p, "resolved")
        else:
            hits = [c for c in correction_candidates(p, directories) if exists(c)]
            if len(hits) == 1:
                res = PathResolution(p, "corrected", corrected_to=hits[0],
                                     source="directory")
            elif len(hits) > 1:
                res = PathResolution(p, "new", ambiguous=hits,
                                     source="directory")
            else:
                # DEV-733: the repository had nothing to say, which is also
                # what a typo in a NEW file's name looks like. The spec did.
                near = declared_near_misses(p, declared, exclude=plan_own)
                if len(near) == 1:
                    res = PathResolution(p, "corrected", corrected_to=near[0],
                                         source="declared")
                elif len(near) > 1:
                    res = PathResolution(p, "new", ambiguous=near,
                                         source="declared")
                else:
                    res = PathResolution(p, "new")
        return res

    for _, _, _, path in entries:
        if path in seen:
            report.resolutions.append(seen[path])
            continue
        # DEV-786: the planner copies change-surface cells WITH their
        # markdown backticks (run 51: five correct paths, each `quoted`).
        # Classify the bare path, and record the unquoting as a correction so
        # the plan the gate sees is the bare path.
        bare = unquote_path(path)
        res = _classify(bare)
        if bare != path and res.status != "placeholder":
            target = res.corrected_to if res.status == "corrected" else bare
            res = PathResolution(path, "corrected", corrected_to=target,
                                 ambiguous=list(res.ambiguous),
                                 source="unquoted" + ("+" + res.source if res.source else ""))
        elif bare != path:
            res = PathResolution(path, "placeholder")
        seen[path] = res
        report.resolutions.append(res)
    return report


def apply_corrections(plan: dict, corrections: dict[str, str]) -> dict:
    """A copy of *plan* with phase paths rewritten. Nothing else is touched."""
    import copy
    if not corrections:
        return plan
    out = copy.deepcopy(plan)
    phases = out.get("phases")
    if not isinstance(phases, list):
        return out
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        for key in PHASE_PATH_KEYS:
            values = phase.get(key)
            if not isinstance(values, list):
                continue
            phase[key] = [
                corrections.get(v.strip(), v) if isinstance(v, str) else v
                for v in values
            ]
    return out
