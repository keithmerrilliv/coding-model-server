"""Per-role RAG opt-in for autonomous agents.

_http.post_chat_completion defaults skip_memory=True for the whole autonomous
package, and no caller ever overrode it — so no autonomous agent had ever
received a retrieved chunk, however good the collection was. These pin the
opt-in and, more importantly, pin that the DEFAULT stays off.

Note: these deliberately do NOT importlib.reload the executor. An earlier
version did, and it broke five unrelated manifest tests in the same session,
because reloading rebinds module-scope objects that other modules already hold
references to. The parsing is tested through _parse_memory_roles and the
behaviour through monkeypatching the module attribute.
"""
from unittest import mock

import pytest

import coding_model_autonomous.executor as ex


def _capture_call(role):
    """Run call_agent and return the kwargs it passed to post_chat_completion."""
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": [{"message": {"content": "ok"},
                                           "finish_reason": "stop"}]}
    with mock.patch.object(ex, "post_chat_completion", return_value=resp) as p:
        ex.call_agent(role, [{"role": "user", "content": "make an MTLDevice"}])
    return p.call_args.kwargs


class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("", set()),
        (None, set()),
        (" , ,", set()),                      # trailing commas must not add ''
        ("implementer", {"implementer"}),
        (" Implementer , ARCHITECT ", {"implementer", "architect"}),
        ("reviewer,reviewer", {"reviewer"}),
    ])
    def test_parse(self, raw, expected):
        assert ex._parse_memory_roles(raw) == expected


class TestDefaultStaysOff:
    def test_shipped_default_is_empty(self):
        """The module constant as actually imported, with no env set."""
        assert ex._parse_memory_roles(__import__("os").getenv(
            "AUTONOMOUS_MEMORY_ROLES", "")) == ex.AUTONOMOUS_MEMORY_ROLES

    def test_every_role_skips_memory_when_set_is_empty(self, monkeypatch):
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", set())
        for role in ("implementer", "architect", "reviewer", "planner", "supervisor"):
            assert _capture_call(role)["skip_memory"] is True, role


class TestOptIn:
    def test_named_role_gets_memory(self, monkeypatch):
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", {"implementer"})
        assert _capture_call("implementer")["skip_memory"] is False

    def test_other_roles_unaffected_by_one_opt_in(self, monkeypatch):
        """Opting the implementer in must not silently enable the planner,
        whose prompts are decomposition text and would retrieve noise."""
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", {"implementer"})
        for role in ("planner", "supervisor", "reviewer", "architect"):
            assert _capture_call(role)["skip_memory"] is True, role

    def test_role_match_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(ex, "AUTONOMOUS_MEMORY_ROLES", {"architect"})
        assert _capture_call("Architect")["skip_memory"] is False
