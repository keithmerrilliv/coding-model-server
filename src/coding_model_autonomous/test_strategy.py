"""The spec's test-strategy block: read it, restore it, judge the plan by it.

Moved out of orchestrator_daemon.py (DEV-837). The operator writes a
``test_strategy`` section in the spec; the planner rewrites the whole spec
into a plan. This module reads the operator's section in the four dialects
real specs use (DEV-712), forces the operator-owned keys back onto the
plan verbatim (DEV-573, DEV-709), and lists what makes a plan's strategy
unrunnable (DEV-426, DEV-625, DEV-630). Nothing here touches the database;
the daemon decides what a problem costs.
"""
from __future__ import annotations

import logging
import re
import textwrap
from typing import NamedTuple

from coding_model_autonomous.context import change_surface

logger = logging.getLogger("orchestrator.test_strategy")


# DEV-426: keys each Apple framework needs before a dispatch can even be built.
# A plan missing these is invalid by construction — it cannot run, and the
# failure surfaces at the test phase, long after design and implementation.
FRAMEWORK_REQUIRED_KEYS = {
    "swift_test": ("repo",),
    "xcodebuild_test": ("repo", "scheme", "filter"),
}

# DEV-712: the heading is prose, not a key. Real specs write `## Test strategy`
# as often as `## test_strategy`, sometimes with a trailing parenthetical
# ("## Test strategy (for the planner — carry these keys through)"). Matching
# only the underscore form disarmed every guard below for 8 specs.
_SPEC_TEST_STRATEGY_RE = re.compile(
    r"^##+[ \t]*test[ _]strategy\b[^\n]*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE)
_SPEC_STRATEGY_YAML_FENCE_RE = re.compile(
    r"```ya?ml[ \t]*\n(.*?)```", re.DOTALL)
_SPEC_STRATEGY_ANY_FENCE_RE = re.compile(
    r"```[ \t]*\n(.*?)```", re.DOTALL)


class SpecStrategy(NamedTuple):
    """What the spec's own test-strategy section yielded.

    DEV-630 at the spec boundary. "The operator declared nothing" and "the
    operator declared something this parser could not read" are different
    facts, and returning {} for both is what let DEV-712 hide: every guard
    keyed on the declaration stood down at once and said nothing.
    """
    keys: dict
    heading: bool   # a test-strategy heading exists in the spec
    reason: str     # why nothing parsed; "" when keys were read or no heading

    @property
    def unreadable(self) -> bool:
        """A section is there and it yielded no keys. Never silent."""
        return self.heading and not self.keys


def _spec_strategy_block(section: str) -> str:
    """The YAML-ish part of a test-strategy section, without trailing prose.

    Three sources, in order of how sure we are about them:

    1. A ```yaml fence, which says what it is.
    2. The leading run of indented or markdown-list lines, stopping at the
       first column-0 prose line. Every Apple spec follows its indented block
       with an explanatory paragraph, and because that paragraph sits at
       column 0 textwrap.dedent finds a common prefix of "" and dedents
       nothing, so the old code fed YAML and prose to safe_load together and
       it raised (DEV-712, 14 specs).
    3. Only then an untagged fence.

    Order matters: several specs put an indented block under the heading and
    a ```-fenced *shell command* further down the same section, and taking
    the first fence of any kind returned the xcodebuild invocation as the
    test strategy.
    """
    fence = _SPEC_STRATEGY_YAML_FENCE_RE.search(section)
    if fence:
        return textwrap.dedent(fence.group(1)).strip()
    kept: list[str] = []
    for line in section.splitlines():
        if not line.strip():
            kept.append(line)
            continue
        if line[0] in " \t" or line.lstrip().startswith(("- ", "* ")):
            kept.append(line)
            continue
        break
    leading = textwrap.dedent("\n".join(kept)).strip()
    if leading:
        return leading
    untagged = _SPEC_STRATEGY_ANY_FENCE_RE.search(section)
    return textwrap.dedent(untagged.group(1)).strip() if untagged else ""


def _spec_strategy_mapping(parsed) -> dict:
    """Coerce a parsed strategy block to a mapping, or {} if it is not one.

    Accepts the markdown-list dialect. `- framework: swift_test` on its own
    line is how an operator writes a mapping in a bullet list, and YAML reads
    it as a sequence of single-key mappings; merging them in order recovers
    exactly what was written. Every Centipede spec uses this form.
    """
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        merged: dict = {}
        for item in parsed:
            if not isinstance(item, dict):
                return {}
            merged.update(item)
        return merged
    return {}


def parse_spec_test_strategy(spec_md: str) -> SpecStrategy:
    """Read the spec's own test-strategy section. Never raises.

    Four dialects are in real use and all four must work: a ```yaml fence, an
    indented block followed by prose, a markdown bullet list, and any of those
    under a prose heading. Before DEV-712 only the first parsed, and the other
    three returned {} — indistinguishable from a spec that declared nothing.
    """
    import yaml as _yaml
    if not spec_md:
        return SpecStrategy({}, False, "")
    match = _SPEC_TEST_STRATEGY_RE.search(spec_md)
    if not match:
        return SpecStrategy({}, False, "")
    block = _spec_strategy_block(match.group(1))
    if not block:
        return SpecStrategy({}, True, "the section is empty")
    try:
        parsed = _yaml.safe_load(block)
    except _yaml.YAMLError as exc:
        return SpecStrategy(
            {}, True,
            f"the section is not valid YAML ({type(exc).__name__})")
    keys = _spec_strategy_mapping(parsed)
    if not keys:
        return SpecStrategy(
            {}, True,
            f"the section parsed as {type(parsed).__name__}, not a mapping")
    return SpecStrategy(keys, True, "")


def spec_declared_test_strategy(spec_md: str) -> dict:
    """The spec's own test-strategy block as a mapping, {} when unreadable.

    Kept as the mapping-only view for callers that cannot act on the reason.
    Anything that can report should use parse_spec_test_strategy and check
    `.unreadable` — an unreadable section is an operator error worth one
    planner round, not a green light (DEV-712).
    """
    return parse_spec_test_strategy(spec_md).keys


# test_strategy keys the operator declares in the spec that must reach the
# dispatch byte-identical. Everything protective hangs off these; run 14b
# (DEV-573) lost protected_paths to the planner's rewrite and a fabricated
# project.pbxproj reached the VM worktree.
# DEV-709 adds `framework`: run 41's planner read `framework: swift_test` and
# emitted `xcodebuild_test` for a SwiftPM package with no .xcodeproj, which
# could not have dispatched at all. The value comes from a closed enumeration
# the operator picks — there is nothing for a model to add to it, and a
# SUBSTITUTED value is worse than a dropped one because it is well-formed and
# plausible and survives every structural check.
OPERATOR_STRATEGY_KEYS = ("repo", "protected_paths", "base_ref", "filter",
                           "execution_target", "framework", "skip_filter",
                           "default_actor_isolation")   # DEV-784


def overlay_operator_test_strategy(yaml_text: str, spec_md: str,
                                    spec_id: str) -> str:
    """Force the spec's operator-authored test_strategy keys onto the plan.

    The plan is an LLM rewrite of the spec, and protection metadata must not
    depend on a model choosing to copy it (DEV-573). For each operator key the
    spec declares, the spec's value wins — missing keys are restored and
    divergent values overwritten, loudly. Returns the (possibly rewritten)
    plan YAML; the original text is kept whenever no overlay is needed so the
    gate shows the planner's own formatting.
    """
    import yaml as _yaml
    spec_strategy = parse_spec_test_strategy(spec_md)
    if spec_strategy.unreadable:
        # DEV-712: the overlay used to stand down here without a word, which
        # is how DEV-573's fix sat disarmed for a month on 23% of specs.
        logger.warning(
            "spec %s: the spec has a test-strategy section but %s — the "
            "DEV-573 overlay has nothing to restore and is NOT armed for "
            "this plan (DEV-712)", spec_id, spec_strategy.reason)
        return yaml_text
    declared = spec_strategy.keys
    wanted = {k: declared[k] for k in OPERATOR_STRATEGY_KEYS if k in declared}
    if not wanted:
        return yaml_text
    try:
        plan = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError:
        return yaml_text  # malformed YAML is rejected downstream, not here
    if not isinstance(plan, dict):
        return yaml_text
    strategy = plan.get("test_strategy")
    if not isinstance(strategy, dict):
        # DEV-630: the one shape the overlay cannot repair. Validation bounces
        # it (below); say here, by name, that nothing was restored.
        logger.warning(
            "spec %s: the plan has no test_strategy mapping (%s) while the "
            "spec declares operator key(s) %s — the DEV-573 overlay cannot "
            "restore them and is NOT armed for this plan (DEV-630)",
            spec_id, type(strategy).__name__, ", ".join(sorted(wanted)))
        return yaml_text
    changed = [k for k, v in wanted.items() if strategy.get(k) != v]
    if not changed:
        return yaml_text
    strategy.update({k: wanted[k] for k in changed})
    logger.warning(
        "spec %s: planner dropped or rewrote operator test_strategy key(s) "
        "%s — restored verbatim from the spec (DEV-573)",
        spec_id, ", ".join(sorted(changed)))
    return _yaml.safe_dump(plan, sort_keys=False)


def validate_test_strategy(yaml_text: str, spec_md: str) -> list[str]:
    """Problems that make a plan's test_strategy unrunnable. Empty means fine.

    Two rules. The framework's own required keys must be present, because
    without them no dispatch can be constructed. And every key the spec's own
    test_strategy block declares must survive into the plan — the planner may
    add keys, never silently drop them. The second rule is the stronger one:
    `base_ref` and `protected_paths` are not framework-required, and losing
    them fails silently rather than loudly (DEV-427 is disabled outright).
    """
    import yaml as _yaml
    try:
        plan = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError:
        return []  # malformed YAML is _bootstrap_tasks' job to reject, not ours
    if not isinstance(plan, dict):
        return []
    # DEV-712: before anything else, say whether the spec's own declaration
    # was readable. If it was not, every rule below is running on {} and the
    # plan cannot be judged against the operator's intent at all. That is a
    # spec defect, and one round naming it costs far less than a run that
    # silently loses its protected paths.
    spec_strategy = parse_spec_test_strategy(spec_md)
    spec_problems: list[str] = []
    if spec_strategy.unreadable:
        spec_problems.append(
            "the spec has a `test_strategy` section but no keys could be read "
            f"from it — {spec_strategy.reason}. Nothing the spec declared is "
            "being enforced: the operator-key overlay, the dropped-key rule "
            "and the repo check are all standing down. Write the block as "
            "`key: value` lines under the heading (a ```yaml fence, an "
            "indented block, or a `- key: value` list all parse) and "
            "resubmit.")
    strategy = plan.get("test_strategy")
    if not isinstance(strategy, dict):
        # DEV-630: with no mapping at all, every rule below stood down at
        # once, including the DEV-573 overlay. When the spec declared
        # operator keys the planner dropped a whole block, and a round to
        # copy it through is exactly what plan validation is for.
        declared = spec_strategy.keys
        wanted = sorted(k for k in OPERATOR_STRATEGY_KEYS if k in declared)
        if wanted:
            return spec_problems + [
                "the plan has no `test_strategy` mapping, but the spec's own "
                "test_strategy block declares "
                + ", ".join(f"`{k}`" for k in wanted)
                + ". Copy the block through as real YAML keys under "
                "`test_strategy:` — the pipeline cannot enforce protection "
                "metadata it cannot read."]
        return spec_problems  # no strategy at all is a different (non-Apple) shape

    problems: list[str] = list(spec_problems)
    reported: set[str] = set()
    framework = str(strategy.get("framework") or "").strip()
    for key in FRAMEWORK_REQUIRED_KEYS.get(framework, ()):
        if not strategy.get(key):
            reported.add(key)
            problems.append(
                f"`{key}` is required for `framework: {framework}` and is missing. "
                f"Without it the runner dispatch cannot be built at all.")

    # A key can fail both rules; say so once.
    declared = spec_strategy.keys
    dropped = [k for k in declared
               if k not in ("framework", "required", "notes")
               and k not in strategy and k not in reported]
    for key in dropped:
        problems.append(
            f"`{key}` is declared in the spec's own test_strategy block and is "
            f"absent from the plan. Copy it through as a real YAML key — "
            f"prose inside `notes` is never parsed.")

    # DEV-625: a spec that modifies existing files needs a repo to read them
    # from. Without this rule the DEV-492 acceptance probe treats a missing
    # repo key as "every declared file is unreadable" and terminally fails
    # the spec before any gate opens — though a planner round fixes a dropped
    # key (run 19's plan gate proved it in one note; run 20 died on it).
    surface = change_surface(spec_md)
    if surface.kind == "unrecognised":
        # DEV-630: the spec has a table and we could not read a path from it.
        # "Nothing declared" and "could not tell" used to be the same [] here,
        # and this guard stood down on both. Arm it by name instead.
        logger.warning(
            "plan validation: the change-surface table has %d row(s) but no "
            "path could be read from any of them — the DEV-492 repo-key check "
            "cannot tell whether existing files are modified and is NOT armed "
            "for this plan (DEV-630)", surface.rows)
    if (not strategy.get("repo")
            and not any("`repo`" in p for p in problems)
            and surface.any):
        problems.append(
            "the spec declares modifications to existing files but the plan's "
            "`test_strategy` has no `repo` key naming the repository to read "
            "them from. Copy the `repo` value from the spec's test_strategy "
            "block through as a real YAML key.")
    return problems
