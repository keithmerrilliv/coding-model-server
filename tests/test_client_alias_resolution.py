"""The interactive client resolves agent aliases.

/v1/models lists only canonical names, so AGENT_THEMES never holds aliases;
without resolve_agent the client would reject `@alias` / `--model alias` that
the server accepts and fall back to implementer. resolve_agent maps an alias in
Config.AGENT_ALIASES to its canonical target, but only when the server actually
offers that target. The roster carries no alias today, so the tests add one.
"""
import pytest

from coding_model_client import models
from coding_model_client.models import resolve_agent
from coding_model_server.config import Config


def _themes(*names):
    return {n: {"desc": n} for n in names}


@pytest.fixture
def legacy_alias(monkeypatch):
    monkeypatch.setitem(Config.AGENT_ALIASES, "legacy_planner", "dense_architect")


def test_alias_resolves_to_canonical(monkeypatch, legacy_alias):
    monkeypatch.setattr(models, "AGENT_THEMES",
                        _themes("dense_architect", "implementer"))
    assert resolve_agent("legacy_planner") == "dense_architect"


def test_canonical_name_is_unchanged(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent("implementer") == "implementer"


def test_unknown_name_is_unchanged(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent("nope") == "nope"


def test_alias_to_unavailable_target_is_not_invented(monkeypatch, legacy_alias):
    # If the server doesn't offer the alias target, don't resolve to it — let
    # the caller's own existence check handle the miss.
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent("legacy_planner") == "legacy_planner"


def test_architect_is_not_an_alias(monkeypatch):
    # Two agents play the architect role, so the bare role name must not
    # quietly pick one of them.
    monkeypatch.setattr(models, "AGENT_THEMES",
                        _themes("dense_architect", "qwen38_architect"))
    assert "architect" not in Config.AGENT_ALIASES
    assert resolve_agent("architect") == "architect"


def test_none_is_safe(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent(None) is None
