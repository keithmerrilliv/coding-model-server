"""Acceptance criteria the pipeline can actually decide (DEV-483).

The pipeline observes exactly one thing about an attempt: the output of one
test-runner dispatch — one worktree at ``base_ref``, the patch applied, one
run. From that it can read the pass/fail of named tests, the run's log, and
the files the implementer emitted. Nothing else: no second run, no git, no
shell, no filesystem beyond the emitted files.

Two ElectricSheep plans (spec_837b167f, spec_f47132ab) were written as if a
developer with a shell would verify them: a pre-fix run that must fail, a
``git status --porcelain`` listing, an exact count of pre-existing warning
lines. Every such criterion is impossible for every implementation, and it
only shows up after design, implementation and a 130-210 s ``xcodebuild_test``
dispatch — up to an hour of Mac time per spec failing on the harness rather
than the code. The planner prompt now states the observation model; this is
the check behind it, run at the plan gate beside DEV-426's test_strategy
validation so a reviewer never spends a round on a plan no run can satisfy.

The patterns are deliberately narrow (DEV-440: a guard that fires on good
plans costs more than it saves). Each hit names the criterion, why the
harness cannot decide it, and the single-run alternative that carries the
same intent.
"""
from __future__ import annotations

import re
from typing import Iterable

import yaml

# Each shape: (name, patterns, why the harness cannot decide it, what to write instead)
_SHAPES: list[tuple[str, list[re.Pattern[str]], str, str]] = [
    (
        "a before/after comparison",
        [re.compile(p, re.IGNORECASE) for p in (
            r"\bpre-?fix\b",
            r"\bpre-?change\b",
            r"\bpre-?patch\b",
            r"\bbefore (?:the|this|any|my|our) (?:change|fix|patch)\b",
            r"\bbefore/after\b",
            r"\bagainst main\b",
            r"\bon (?:unpatched|unmodified) main\b",
            r"\bwithout (?:the|this) (?:fix|change|patch)\b",
            r"\bwith (?:the|this) (?:fix|change|patch) reverted\b",
            r"\bbaseline run\b",
            r"\bno new (?:warnings?|diagnostics?|errors?)\b",
        )],
        "the runner materialises one worktree at base_ref, applies the patch and "
        "runs once; there is no pre-fix run, and a build failure asked for as a "
        "passing condition fights the pre-gate build check",
        "assert directly on the property the bug violated, in one run — a test "
        "that would have failed on the old code proves the fix is not vacuous "
        "without a second dispatch",
    ),
    (
        "git or worktree inspection",
        [re.compile(p, re.IGNORECASE) for p in (
            r"\bgit (?:status|diff|log|show|ls-files|stash|blame)\b",
            r"--porcelain\b",
            r"\b--name-only\b",
            r"\bworking tree\b",
            r"\buntracked files?\b",
        )],
        "the reviewer has no shell on the runner and no channel reports worktree "
        "state; the only files it can see are the ones the implementer emitted",
        "fence the files that must not change with test_strategy.protected_paths, "
        "which the pipeline enforces mechanically, and name the files that must "
        "appear as phase outputs",
    ),
    (
        "an exact count of incidental output",
        [re.compile(p, re.IGNORECASE) for p in (
            r"\bexactly (?:\d+|one|two|three|four|five|no|zero) (?:pre-existing |existing |incidental |other )?"
            r"(?:warnings?|diagnostics?|deprecations?)(?: lines?)?\b",
            r"\bcount\s*=\s*\d+\b",
            r"\boccurs? exactly (?:\d+|once|twice)\b",
            r"\bexactly (?:\d+|one|two|three) (?:occurrences?|matches|instances?) of\b",
        )],
        "a correct implementation that checks the same thing in two places fails "
        "it, and diagnostics outside the changed files are not under the "
        "implementer's control",
        "name the test cases that must pass and, for cleanliness, require the "
        "build to succeed; leave counts of incidental output out",
    ),
]


def plan_criteria(yaml_text: str) -> list[str]:
    """The plan's acceptance_criteria as strings; [] when absent or unparseable.
    A malformed plan is DEV-426's problem, not this check's."""
    try:
        plan = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return []
    if not isinstance(plan, dict):
        return []
    raw = plan.get("acceptance_criteria")
    if not isinstance(raw, list):
        return []
    return [str(c).strip() for c in raw if c]


def unevaluable_criteria(yaml_text: str) -> list[str]:
    """One problem line per criterion the harness cannot decide, each naming
    the criterion, the reason and the single-run alternative. [] for a plan
    whose criteria are all decidable from one run."""
    return list(_judge(plan_criteria(yaml_text)))


def _judge(criteria: Iterable[str]) -> Iterable[str]:
    for i, text in enumerate(criteria, start=1):
        for name, patterns, why, instead in _SHAPES:
            hit = next((m.group(0) for p in patterns if (m := p.search(text))), None)
            if hit is None:
                continue
            shown = text if len(text) <= 100 else text[:97] + "..."
            yield (f"acceptance criterion {i} asks for {name} (\"{hit}\"): "
                   f"\"{shown}\" — {why}. Instead, {instead}.")
            break  # one reason per criterion is enough to act on


REJECTION_GUIDANCE = (
    "One or more acceptance criteria ask for evidence the pipeline cannot "
    "produce. The pipeline observes exactly one test-runner dispatch: one "
    "worktree at base_ref, the patch applied, one run. A criterion may rest "
    "on the pass/fail of named tests in that run, the contents of its log, "
    "and the files the implementer emitted — nothing else. Rewrite each "
    "criterion below as a condition one run decides; the alternative given "
    "with each keeps its intent. Do not drop the intent, and do not move the "
    "criterion into `notes`."
)
