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
                       extra_directories: Iterable[str] = ()) -> PlanPathReport:
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

    seen: dict[str, PathResolution] = {}
    for _, _, _, path in entries:
        if path in seen:
            report.resolutions.append(seen[path])
            continue
        if is_placeholder(path):
            res = PathResolution(path, "placeholder")
        elif is_run_artifact(path) or exists(path):
            res = PathResolution(path, "resolved")
        else:
            hits = [c for c in correction_candidates(path, directories) if exists(c)]
            if len(hits) == 1:
                res = PathResolution(path, "corrected", corrected_to=hits[0])
            elif len(hits) > 1:
                res = PathResolution(path, "new", ambiguous=hits)
            else:
                res = PathResolution(path, "new")
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
