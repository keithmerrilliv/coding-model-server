"""DEV-573: operator-authored test_strategy keys must survive the planner.

Run 14b lost `protected_paths` to the planner's rewrite and a fabricated
project.pbxproj reached the VM worktree. Two defects cooperated: the spec's
test_strategy parser could not read the ```yaml fence every real spec uses
(so DEV-426's dropped-key rule was vacuously satisfied), and nothing restored
the keys mechanically.
"""

from coding_model_server.orchestrator_daemon import (
    _overlay_operator_test_strategy,
    _spec_declared_test_strategy,
)

import yaml

FENCED_SPEC = """# Some spec

## test_strategy

```yaml
framework: xcodebuild_test
required: true
repo: electric-sheep
scheme: ElectricSheep
destination: "platform=macOS"
filter: ElectricSheepTests
execution_target: client
protected_paths:
  - ElectricSheep.xcodeproj/project.pbxproj
  - ElectricSheepTests/ForcingStrategyTests.swift
```

Trailing prose after the fence.
"""

INDENTED_SPEC = """# Some spec

## test_strategy

    framework: pytest
    required: true
    base_ref: main

## Next section
"""

# The planner's rewrite: framework keys kept, operator keys demoted to prose —
# the exact shape both run-14 plans produced.
DROPPED_PLAN = """\
title: t
test_strategy:
  framework: xcodebuild_test
  required: true
  repo: electric-sheep
  scheme: ElectricSheep
  destination: "platform=macOS"
  filter: ElectricSheepTests
"""


class TestFencedSpecParsing:
    def test_fenced_block_is_parsed(self):
        declared = _spec_declared_test_strategy(FENCED_SPEC)
        assert declared["framework"] == "xcodebuild_test"
        assert declared["protected_paths"] == [
            "ElectricSheep.xcodeproj/project.pbxproj",
            "ElectricSheepTests/ForcingStrategyTests.swift",
        ]
        assert declared["execution_target"] == "client"

    def test_indented_block_still_parsed(self):
        declared = _spec_declared_test_strategy(INDENTED_SPEC)
        assert declared["framework"] == "pytest"
        assert declared["base_ref"] == "main"

    def test_run_14_spec_regression(self):
        # The shipped spec that exposed the inert parser must now parse.
        md = open("docs/specs/electric_sheep_mlx_error_containment.md").read()
        declared = _spec_declared_test_strategy(md)
        assert "protected_paths" in declared
        assert len(declared["protected_paths"]) == 4


class TestOverlay:
    def test_dropped_keys_are_restored(self):
        out = _overlay_operator_test_strategy(DROPPED_PLAN, FENCED_SPEC, "s1")
        strategy = yaml.safe_load(out)["test_strategy"]
        assert strategy["protected_paths"] == [
            "ElectricSheep.xcodeproj/project.pbxproj",
            "ElectricSheepTests/ForcingStrategyTests.swift",
        ]
        assert strategy["execution_target"] == "client"
        # Planner-owned keys are untouched.
        assert strategy["scheme"] == "ElectricSheep"

    def test_divergent_value_is_overwritten(self):
        plan = DROPPED_PLAN + "  execution_target: server\n"
        out = _overlay_operator_test_strategy(plan, FENCED_SPEC, "s1")
        assert yaml.safe_load(out)["test_strategy"]["execution_target"] == "client"

    def test_no_overlay_keeps_original_text(self):
        # All operator keys present and equal: the planner's own formatting
        # (and the gate's rendering of it) must be preserved byte-for-byte.
        plan = DROPPED_PLAN + (
            "  execution_target: client\n"
            "  protected_paths:\n"
            "  - ElectricSheep.xcodeproj/project.pbxproj\n"
            "  - ElectricSheepTests/ForcingStrategyTests.swift\n"
        )
        assert _overlay_operator_test_strategy(plan, FENCED_SPEC, "s1") == plan

    def test_spec_without_strategy_is_noop(self):
        assert _overlay_operator_test_strategy(
            DROPPED_PLAN, "# no strategy here", "s1") == DROPPED_PLAN

    def test_malformed_plan_is_left_for_downstream(self):
        assert _overlay_operator_test_strategy(
            ":\nnot yaml [", FENCED_SPEC, "s1") == ":\nnot yaml ["
