"""DEV-723 — the eval harness survives a transient 5xx and gives thinking-on
models a real budget.

Two defects made the harness unusable for the architect slot once thinking-on
became policy:

  * `--max-tokens` defaulted to 1400 while production gives the architect 8000.
    Reasoning and the answer share that budget (DEV-556), so 1400 either
    truncated the answer or was consumed entirely by reasoning, which the
    server rejects with a "reasoning-only" 502.
  * `_completion` called `raise_for_status()` with no retry, so ONE 5xx killed
    a run that batches ~40 minutes of generation before it judges anything.
"""
import importlib.util
import os

import pytest
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "..", "scripts", "eval_agents.py")


@pytest.fixture(scope="module")
def ea():
    spec = importlib.util.spec_from_file_location("eval_agents_resilience", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    """Minimal requests.Response stand-in."""

    def __init__(self, status, text="", body=None):
        self.status_code = status
        self.text = text
        self._body = body or {"choices": [{"message": {"content": "ok"}}],
                              "usage": {"completion_tokens": 5}}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Error", response=self)


def _no_sleep(monkeypatch, ea):
    monkeypatch.setattr(ea.time, "sleep", lambda *_: None)


# ── the budget ───────────────────────────────────────────────────────────────

def test_the_default_budget_matches_production(ea):
    """1400 truncated answers and caused reasoning-only 502s. The eval must not
    measure a model under a budget production never imposes, so the default is
    IMPORTED from the pipeline rather than copied — if production's architect
    budget moves and the harness does not, this fails."""
    from coding_model_autonomous.executor import ARCHITECT_MAX_TOKENS

    assert ea.DEFAULT_MAX_TOKENS == ARCHITECT_MAX_TOKENS
    assert ea.DEFAULT_MAX_TOKENS >= 6083, (
        "DEV-702's largest single completion was 6,083 tokens; a smaller "
        "budget silently truncates that task")


# ── retry on transient failure ───────────────────────────────────────────────

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_5xx_is_retried_then_succeeds(ea, monkeypatch, status):
    _no_sleep(monkeypatch, ea)
    calls = []

    def fake_post(*a, **kw):
        calls.append(1)
        return _Resp(status, "upstream hiccup") if len(calls) == 1 else _Resp(200)

    monkeypatch.setattr(ea.requests, "post", fake_post)
    text, tokens = ea._completion("http://s", {}, "architect", [], 8000)
    assert (text, tokens) == ("ok", 5)
    assert len(calls) == 2, "should have retried exactly once"


def test_a_connection_error_is_retried(ea, monkeypatch):
    _no_sleep(monkeypatch, ea)
    calls = []

    def fake_post(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("reset")
        return _Resp(200)

    monkeypatch.setattr(ea.requests, "post", fake_post)
    assert ea._completion("http://s", {}, "architect", [], 8000)[0] == "ok"
    assert len(calls) == 3


def test_it_gives_up_after_the_cap_rather_than_looping(ea, monkeypatch):
    _no_sleep(monkeypatch, ea)
    calls = []
    monkeypatch.setattr(ea.requests, "post",
                        lambda *a, **kw: (calls.append(1), _Resp(502, "still down"))[1])
    with pytest.raises(requests.exceptions.HTTPError):
        ea._completion("http://s", {}, "architect", [], 8000)
    assert len(calls) == ea.COMPLETION_ATTEMPTS


# ── the negative control ─────────────────────────────────────────────────────

def test_a_4xx_fails_fast_and_is_NOT_retried(ea, monkeypatch):
    """A bad agent name or a bad key must surface immediately. Retrying a 4xx
    would turn an instant, readable failure into four sleeps and a timeout —
    and would make the retry test above pass for the wrong reason."""
    _no_sleep(monkeypatch, ea)
    calls = []
    monkeypatch.setattr(ea.requests, "post",
                        lambda *a, **kw: (calls.append(1), _Resp(404, "no such model"))[1])
    with pytest.raises(requests.exceptions.HTTPError):
        ea._completion("http://s", {}, "nope", [], 8000)
    assert len(calls) == 1, "a 4xx must not be retried"
