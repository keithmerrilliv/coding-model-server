"""DEV-625: a modification spec whose plan lacks test_strategy.repo must
bounce to the planner, never terminally fail at acceptance.

Run 20 (spec_b367b00f) died before any gate: the planner dropped `repo`,
pytest's framework rules don't require it, and the DEV-492 probe then read
"no repo" as "every declared file is unreadable" — a terminal block for a
one-note fix. Also: `repo` joins _OPERATOR_STRATEGY_KEYS so a spec-declared
value is restored mechanically (DEV-573 overlay) instead of costing a round.
"""
import coding_model_server.orchestrator_daemon as d

MODIFY_SPEC = """
## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/executor.py` | modify — add guards |

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_client/
```
"""

GREENFIELD_SPEC = """
## Change surface

All files are new; no existing file is modified.
"""

PLAN_NO_REPO = """
title: "Guards"
test_strategy:
  framework: pytest
  required: true
"""

PLAN_WITH_REPO = PLAN_NO_REPO + "  repo: coding-model-server\n"


class TestRepoValidationRule:
    def test_modification_spec_without_repo_is_a_problem(self):
        problems = d._validate_test_strategy(PLAN_NO_REPO, MODIFY_SPEC)
        assert any("`repo`" in p for p in problems)

    def test_repo_present_is_fine(self):
        # The spec-declared repo also survives via the overlay; validate the
        # plan as the overlay would emit it.
        merged = d._overlay_operator_test_strategy(
            PLAN_NO_REPO, MODIFY_SPEC, "spec_test")
        assert "coding-model-server" in merged
        assert d._validate_test_strategy(merged, MODIFY_SPEC) == []

    def test_greenfield_without_repo_is_fine(self):
        assert d._validate_test_strategy(PLAN_NO_REPO, GREENFIELD_SPEC) == []

    def test_no_double_report_when_dropped_rule_already_fired(self):
        problems = d._validate_test_strategy(PLAN_NO_REPO, MODIFY_SPEC)
        assert sum("`repo`" in p for p in problems) == 1


class TestRepoOverlay:
    def test_spec_declared_repo_is_restored_mechanically(self):
        merged = d._overlay_operator_test_strategy(
            PLAN_NO_REPO, MODIFY_SPEC, "spec_test")
        import yaml
        strategy = yaml.safe_load(merged)["test_strategy"]
        assert strategy["repo"] == "coding-model-server"
        assert strategy["protected_paths"] == ["src/coding_model_client/"]

    def test_plan_with_matching_repo_is_left_verbatim(self):
        merged = d._overlay_operator_test_strategy(
            PLAN_WITH_REPO, MODIFY_SPEC, "spec_test")
        # protected_paths still gets restored, so the text may change; but a
        # matching repo alone must not rewrite the plan when nothing differs.
        import yaml
        assert yaml.safe_load(merged)["test_strategy"]["repo"] == "coding-model-server"
