"""The interactive client resolves legacy agent aliases.

/v1/models lists only canonical names, so AGENT_THEMES never holds aliases, and
the client used to reject `@architect` / `--model architect` and fall back to
implementer — contradicting the documented aliases (Config.AGENT_ALIASES).
resolve_agent maps a documented alias to its canonical target, but only when the
server actually offers that target.
"""
from coding_model_client import models
from coding_model_client.models import resolve_agent


def _themes(*names):
    return {n: {"desc": n} for n in names}


def test_alias_resolves_to_canonical(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES",
                        _themes("dense_architect", "implementer"))
    # `architect` -> `dense_architect` (Config.AGENT_ALIASES)
    assert resolve_agent("architect") == "dense_architect"


def test_canonical_name_is_unchanged(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent("implementer") == "implementer"


def test_unknown_name_is_unchanged(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent("nope") == "nope"


def test_alias_to_unavailable_target_is_not_invented(monkeypatch):
    # If the server doesn't offer the alias target, don't resolve to it — let
    # the caller's own existence check handle the miss.
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent("architect") == "architect"


def test_none_is_safe(monkeypatch):
    monkeypatch.setattr(models, "AGENT_THEMES", _themes("implementer"))
    assert resolve_agent(None) is None
