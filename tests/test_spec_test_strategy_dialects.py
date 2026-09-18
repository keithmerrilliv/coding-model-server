"""DEV-712: the spec's test-strategy section must parse in every real dialect.

`_spec_declared_test_strategy` is the single source for "what did the operator
declare". The DEV-573 overlay, the DEV-426 dropped-key rule and the
DEV-492/DEV-625 repo check all hang off it, and when it returned {} all three
stood down at once without a word.

It returned {} for 22 of 96 specs in the tree. The parser was fine; its idea of
the input dialect was wrong. Four forms are in real use:

  1. a ```yaml fence                      (what the DEV-573 test used)
  2. an indented block followed by prose  (every ElectricSheep spec)
  3. a markdown bullet list               (every Centipede spec)
  4. any of those under a prose heading   ("## Test strategy")

Only (1) parsed. That is why DEV-438 and DEV-573 both shipped a fix for this
symptom and DEV-699 and DEV-709 are the same symptom a month later.
"""

from pathlib import Path

import pytest
import yaml

from coding_model_server.orchestrator_daemon import (
    _overlay_operator_test_strategy,
    _parse_spec_test_strategy,
    _spec_declared_test_strategy,
    _validate_test_strategy,
)

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# The four dialects
# --------------------------------------------------------------------------

FENCED = """# Spec

## test_strategy

```yaml
framework: xcodebuild_test
repo: electric-sheep
protected_paths:
  - ElectricSheep.xcodeproj/project.pbxproj
```

Trailing prose after the fence.
"""

# The ElectricSheep form: indented YAML, then an explanatory paragraph at
# column 0. dedent finds a common prefix of "" and dedents nothing, so the old
# code handed YAML and prose to safe_load together and it raised.
INDENTED_THEN_PROSE = """# Spec

## test_strategy

    framework: xcodebuild_test
    repo: electric-sheep
    filter: ElectricSheepTests

The `filter` key is REQUIRED: it becomes an `-only-testing:` selector.
Without it xcodebuild runs every target in the scheme.

## Next section
"""

# The Centipede form. YAML reads this as a sequence of single-key mappings.
BULLET_LIST = """# Spec

## Test strategy

- framework: swift_test
- repo: centipede
- base_ref: main
- execution_target: client
- protected_paths:
  - Package.swift
  - Sources/CentipedeCore/World.swift

## Risks
"""

PARENTHETICAL_HEADING = """# Spec

## Test strategy (for the planner — carry these keys through)

```yaml
framework: pytest
repo: coding-model-server
```
"""

# An indented block under the heading and a fenced *shell command* further
# down the same section. Taking the first fence of any kind returned the
# xcodebuild invocation as the test strategy.
INDENTED_THEN_SHELL_FENCE = """# Spec

## test_strategy

    framework: xcodebuild_test
    repo: electric-sheep

Canonical command, which is exactly what the above keys must produce:

```
xcodebuild test \\
  -project ElectricSheep.xcodeproj \\
  -scheme ElectricSheep
```
"""


@pytest.mark.parametrize("spec_md,expected", [
    pytest.param(FENCED, {"framework": "xcodebuild_test",
                          "repo": "electric-sheep"}, id="fenced-yaml"),
    pytest.param(INDENTED_THEN_PROSE, {"framework": "xcodebuild_test",
                                       "repo": "electric-sheep",
                                       "filter": "ElectricSheepTests"},
                 id="indented-then-prose"),
    pytest.param(BULLET_LIST, {"framework": "swift_test",
                               "repo": "centipede",
                               "base_ref": "main",
                               "execution_target": "client"},
                 id="markdown-bullet-list"),
    pytest.param(PARENTHETICAL_HEADING, {"framework": "pytest",
                                         "repo": "coding-model-server"},
                 id="prose-heading-with-parenthetical"),
    pytest.param(INDENTED_THEN_SHELL_FENCE, {"framework": "xcodebuild_test",
                                             "repo": "electric-sheep"},
                 id="indented-beats-a-later-shell-fence"),
])
def test_every_real_dialect_parses(spec_md, expected):
    declared = _spec_declared_test_strategy(spec_md)
    for key, value in expected.items():
        assert declared.get(key) == value, f"{key} lost from {declared!r}"


def test_bullet_list_keeps_a_nested_sequence():
    """protected_paths is the key everything protective hangs off."""
    declared = _spec_declared_test_strategy(BULLET_LIST)
    assert declared["protected_paths"] == [
        "Package.swift", "Sources/CentipedeCore/World.swift"]


def test_shell_fence_is_not_read_as_the_strategy():
    declared = _spec_declared_test_strategy(INDENTED_THEN_SHELL_FENCE)
    assert "xcodebuild test" not in repr(declared)


# --------------------------------------------------------------------------
# Absent is not unknown (DEV-630 applied at the spec boundary)
# --------------------------------------------------------------------------

def test_no_section_at_all_is_silent():
    """A spec with no strategy section declared nothing. That is not an error."""
    parsed = _parse_spec_test_strategy("# Spec\n\n## Goal\n\nDo a thing.\n")
    assert parsed.keys == {}
    assert parsed.heading is False
    assert parsed.unreadable is False
    assert parsed.reason == ""


def test_unreadable_section_names_a_reason():
    """A section that yields nothing must never look like "declared nothing"."""
    prose = """# Spec

## Test strategy (for the planner — carry these keys through)

- `repo`: coding-model-server (self)
- `protected_paths`: as listed in "Protected paths" above
"""
    parsed = _parse_spec_test_strategy(prose)
    assert parsed.keys == {}
    assert parsed.heading is True
    assert parsed.unreadable is True
    assert parsed.reason, "an unreadable section must say why"


def test_readable_section_is_not_flagged_unreadable():
    """Negative control for the rule above."""
    assert _parse_spec_test_strategy(BULLET_LIST).unreadable is False


def test_validation_bounces_a_spec_whose_section_yields_nothing():
    prose_spec = """# Spec

## Test strategy

Use pytest and do not touch the protected files listed above.
"""
    plan = "title: t\ntest_strategy:\n  framework: pytest\n  repo: x\n"
    problems = _validate_test_strategy(plan, prose_spec)
    assert any("no keys could be read" in p for p in problems), problems


def test_validation_is_quiet_when_the_section_reads():
    """Negative control: a readable section raises no DEV-712 problem."""
    plan = yaml.safe_dump({"title": "t", "test_strategy": {
        "framework": "swift_test", "repo": "centipede", "base_ref": "main",
        "execution_target": "client",
        "protected_paths": ["Package.swift",
                            "Sources/CentipedeCore/World.swift"]}})
    problems = _validate_test_strategy(plan, BULLET_LIST)
    assert not any("no keys could be read" in p for p in problems), problems


# --------------------------------------------------------------------------
# DEV-699 — the dropped keys run 39 lost
# --------------------------------------------------------------------------

def test_dev699_overlay_restores_keys_the_planner_demoted_to_prose():
    """Run 39's exact shape: the spec declares them, the plan has them in notes.

    The overlay has existed since DEV-573. It never fired on run 39 because
    the spec used the bullet-list dialect under a prose heading, so there was
    nothing for it to restore from.
    """
    plan = """title: slice 8
test_strategy:
  framework: swift_test
  repo: centipede
  notes: |
    Protected paths must remain untouched: Package.swift, World.swift.
"""
    out = _overlay_operator_test_strategy(plan, BULLET_LIST, "spec_test")
    restored = yaml.safe_load(out)["test_strategy"]
    assert restored["protected_paths"] == [
        "Package.swift", "Sources/CentipedeCore/World.swift"]
    assert restored["base_ref"] == "main"
    assert restored["execution_target"] == "client"


def test_dev699_overlay_leaves_a_faithful_plan_alone():
    """Negative control: nothing to restore means the planner's text survives."""
    plan = yaml.safe_dump({"title": "t", "test_strategy": {
        "framework": "swift_test", "repo": "centipede", "base_ref": "main",
        "execution_target": "client",
        "protected_paths": ["Package.swift",
                            "Sources/CentipedeCore/World.swift"]}})
    assert _overlay_operator_test_strategy(plan, BULLET_LIST, "spec_test") == plan


# --------------------------------------------------------------------------
# DEV-709 — the substituted framework run 41 caught at a human gate
# --------------------------------------------------------------------------

def test_dev709_substituted_framework_is_forced_back():
    """The spec said swift_test; the planner said xcodebuild_test.

    Centipede is a SwiftPM package with no .xcodeproj, so the test phase
    could not have run at all. A substituted value is worse than a dropped
    one: it is well-formed and plausible and passes every structural check.
    """
    plan = """title: slice 9
test_strategy:
  framework: xcodebuild_test
  repo: centipede
  scheme: CentipedeCore
"""
    out = _overlay_operator_test_strategy(plan, BULLET_LIST, "spec_test")
    assert yaml.safe_load(out)["test_strategy"]["framework"] == "swift_test"


def test_dev709_agreeing_framework_is_left_alone():
    """Negative control: the guard must not fire when the planner agrees."""
    plan = yaml.safe_dump({"title": "t", "test_strategy": {
        "framework": "swift_test", "repo": "centipede", "base_ref": "main",
        "execution_target": "client",
        "protected_paths": ["Package.swift",
                            "Sources/CentipedeCore/World.swift"]}})
    assert _overlay_operator_test_strategy(plan, BULLET_LIST, "spec_test") == plan


# --------------------------------------------------------------------------
# The spec-archive check that would have caught this on day one
# --------------------------------------------------------------------------

# One tracked spec declares its strategy as prose with backticked keys and a
# value of "as listed in 'Protected paths' above". There is nothing
# machine-readable there, and inventing a parse for it would let the overlay
# force a prose string onto the plan as protected_paths. It is listed here so
# the rule below stays exact, and the test asserts it is FLAGGED, never silent.
KNOWN_PROSE_ONLY = {"dev602_reviewer_overwrite_containment.md"}


def _tracked_specs():
    return sorted((REPO / "docs" / "specs").glob("*.md"))


def test_every_tracked_spec_with_a_strategy_section_parses():
    """23% of the spec archive disarmed every guard, and no test noticed."""
    assert _tracked_specs(), "no specs found — the archive check is vacuous"
    unreadable = []
    for path in _tracked_specs():
        parsed = _parse_spec_test_strategy(
            path.read_text(encoding="utf-8", errors="replace"))
        if parsed.heading and not parsed.keys:
            unreadable.append((path.name, parsed.reason))
    unexpected = [u for u in unreadable if u[0] not in KNOWN_PROSE_ONLY]
    assert not unexpected, (
        "specs with a test-strategy section that yields no keys — every guard "
        f"keyed on the declaration is disarmed for these: {unexpected}")


def test_the_known_prose_spec_is_flagged_rather_than_silent():
    """The allowlist above must never become a silent disarm."""
    for name in KNOWN_PROSE_ONLY:
        path = REPO / "docs" / "specs" / name
        if not path.exists():        # spec retired; nothing to assert
            continue
        parsed = _parse_spec_test_strategy(
            path.read_text(encoding="utf-8", errors="replace"))
        assert parsed.unreadable is True
        assert parsed.reason, f"{name} disarms without saying why"
