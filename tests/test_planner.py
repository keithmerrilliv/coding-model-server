"""Tests for the autonomous planner — parse-retry robustness + the YAML
regression that motivated it (spec_8df49fb3, the Roman-numeral smoke).

The orchestrator turns a PlannerError into spec FAILED, so a single garbled
model emission used to hard-kill a spec. call_planner now re-issues the request
a bounded number of times on a parse failure (mirrors REVIEWER_PARSE_RETRIES).
"""
from coding_model_autonomous import planner


# A valid YAML plan block.
GOOD_YAML = (
    "<<<YAML>>>\n"
    "title: Roman Numeral Converter\n"
    "goal: convert ints to roman numerals\n"
    "language: python\n"
    "<<<END>>>\n"
)

# The EXACT failure from the live smoke run: an unquoted value containing a
# `: ` (colon-space), which YAML parses as a nested mapping → "mapping values
# are not allowed here". This is what killed spec_8df49fb3.
BAD_YAML = (
    "<<<YAML>>>\n"
    "title: Roman Numeral Converter\n"
    "notes: Out-of-scope: CLI wrapper, packaging, type stubs\n"
    "<<<END>>>\n"
)

CLARIFY = (
    "<<<CLARIFY>>>\n"
    "1. Which Python version should this target?\n"
    "<<<END>>>\n"
)


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _seq_completion(monkeypatch, contents, capture=None):
    """Patch post_chat_completion to return _FakeResp for each successive
    content in *contents* (clamping to the last once exhausted). Returns a
    dict tracking the call count; optionally appends each call's kwargs to
    *capture*."""
    state = {"n": 0}

    def fake(*args, **kwargs):
        i = state["n"]
        state["n"] += 1
        if capture is not None:
            capture.append(kwargs)
        return _FakeResp(contents[min(i, len(contents) - 1)])

    monkeypatch.setattr(planner, "post_chat_completion", fake)
    return state


def test_parse_rejects_unquoted_colon_value():
    """Regression: the smoke-test YAML must be detected as unparseable."""
    result = planner.parse_planner_response(BAD_YAML)
    assert isinstance(result, planner.PlannerError)
    assert "not valid YAML" in result.reason


def test_call_planner_retries_bad_yaml_then_succeeds(monkeypatch):
    state = _seq_completion(monkeypatch, [BAD_YAML, GOOD_YAML])
    result = planner.call_planner("# Spec", parse_retries=1)
    assert isinstance(result, planner.PlannerYaml)
    assert state["n"] == 2  # bad → re-roll → good


def test_call_planner_error_after_exhausting_retries(monkeypatch):
    state = _seq_completion(monkeypatch, [BAD_YAML])  # always bad
    result = planner.call_planner("# Spec", parse_retries=2)
    assert isinstance(result, planner.PlannerError)
    assert "not valid YAML" in result.reason
    assert state["n"] == 3  # 1 initial + 2 retries


def test_call_planner_no_retry_on_valid_yaml(monkeypatch):
    state = _seq_completion(monkeypatch, [GOOD_YAML, BAD_YAML])
    result = planner.call_planner("# Spec", parse_retries=3)
    assert isinstance(result, planner.PlannerYaml)
    assert state["n"] == 1  # valid first try short-circuits


def test_call_planner_no_retry_on_clarify(monkeypatch):
    state = _seq_completion(monkeypatch, [CLARIFY, GOOD_YAML])
    result = planner.call_planner("# Spec", parse_retries=3)
    assert isinstance(result, planner.PlannerClarify)
    assert state["n"] == 1  # CLARIFY is a valid outcome, not an error


def test_call_planner_passes_retry_5xx(monkeypatch):
    capture: list[dict] = []
    _seq_completion(monkeypatch, [GOOD_YAML], capture=capture)
    planner.call_planner("# Spec", parse_retries=0)
    assert capture and capture[0].get("retry_5xx") is True


def test_call_planner_uses_module_default_retries(monkeypatch):
    """parse_retries=None falls back to PLANNER_PARSE_RETRIES."""
    monkeypatch.setattr(planner, "PLANNER_PARSE_RETRIES", 1)
    state = _seq_completion(monkeypatch, [BAD_YAML])
    result = planner.call_planner("# Spec")  # no parse_retries → default 1
    assert isinstance(result, planner.PlannerError)
    assert state["n"] == 2  # 1 initial + 1 default retry
