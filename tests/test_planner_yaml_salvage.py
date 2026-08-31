"""DEV-619: colon-rich scalars must not kill a plan.

Run 19's first spec (spec_27b1959f) died terminally because the planner
inlined `_write_artifact(spec_dir: Path, ...)` as an unquoted YAML scalar
and the retry regurgitated the identical structure. Two independent guards:
a mark-guided salvage pass that requotes the offending line, and a retry
that is told what broke.
"""
import yaml

from coding_model_autonomous import planner
from coding_model_autonomous.planner import (
    PlannerError,
    PlannerYaml,
    parse_planner_response,
    call_planner,
)

# The run-19 failure shape: a signature with internal colons, unquoted.
POISONED = """<<<YAML>>>
title: "Guards"
goal: |
  Close the overwrite hole.
language: python
success: Architecture covering _write_artifact(spec_dir: Path, rel_path: str) -> Path enforcement
<<<END>>>"""

VALID = """<<<YAML>>>
title: "Guards"
goal: |
  Close the overwrite hole.
language: python
<<<END>>>"""

GARBAGE = """<<<YAML>>>
title: "x
  : : {{{{
<<<END>>>"""


class TestSalvage:
    def test_run19_signature_line_is_salvaged(self):
        result = parse_planner_response(POISONED)
        assert isinstance(result, PlannerYaml)
        parsed = yaml.safe_load(result.yaml_text)
        assert "spec_dir: Path" in parsed["success"]
        assert parsed["title"] == "Guards"

    def test_valid_yaml_is_untouched(self):
        result = parse_planner_response(VALID)
        assert isinstance(result, PlannerYaml)
        assert "success" not in result.yaml_text  # nothing invented
        assert yaml.safe_load(result.yaml_text)["language"] == "python"

    def test_unsalvageable_garbage_still_errors(self):
        result = parse_planner_response(GARBAGE)
        assert isinstance(result, PlannerError)
        assert "not valid YAML" in result.reason

    def test_salvage_does_not_requote_block_literals(self):
        # goal's block literal contains colons legally; salvage never touches it.
        text = POISONED
        result = parse_planner_response(text)
        assert isinstance(result, PlannerYaml)
        assert "goal: |" in result.yaml_text


class TestRetryFeedback:
    def test_retry_message_names_the_failure(self, monkeypatch):
        seen: list = []

        def fake_call(user_msg, *, agent, timeout):
            seen.append(user_msg)
            if len(seen) == 1:
                return PlannerError(
                    reason="<<<YAML>>> block is not valid YAML: boom",
                    raw_response="")
            return parse_planner_response(VALID)

        monkeypatch.setattr(planner, "_call_planner_once", fake_call)
        result = call_planner("# Spec\nBuild the thing.", parse_retries=1)
        assert isinstance(result, PlannerYaml)
        assert len(seen) == 2
        assert "PREVIOUS ATTEMPT REJECTED" in seen[1]
        assert "boom" in seen[1]
        assert "PREVIOUS ATTEMPT REJECTED" not in seen[0]

    def test_success_never_appends_feedback(self, monkeypatch):
        seen: list = []

        def fake_call(user_msg, *, agent, timeout):
            seen.append(user_msg)
            return parse_planner_response(VALID)

        monkeypatch.setattr(planner, "_call_planner_once", fake_call)
        result = call_planner("# Spec\nBuild.", parse_retries=1)
        assert isinstance(result, PlannerYaml)
        assert len(seen) == 1
