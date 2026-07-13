"""Unit tests for the prompt-budget clamp in routes/chat.py.

The clamp leaves no headroom of its own — it hands the whole remaining window
to the output — so every token that reaches llama-server has to be counted
before it runs. Two things are appended/dropped outside the estimator's view:

  - TOKEN_BUDGET_GUIDANCE is appended to the system prompt *after* the clamp
    (it interpolates the clamped figure). Unreserved, those tokens get handed
    to the output and prompt+output overruns n_ctx — llama-server then stops
    at the KV boundary and the response is silently truncated mid-symbol with
    finish_reason "length".
  - _build_openai_messages drops the agent system prompt entirely when the
    client supplies its own system message, so counting it there under-allocates
    the budget by the size of a prompt that never ships.
"""
import pytest

from coding_model_server.config import Config
from coding_model_server.routes.chat import (
    _estimate_and_clamp_tokens,
    _guidance_token_cost,
)


class _Msg:
    def __init__(self, role, content):
        self.role = role
        self.content = content


def _fake_tokenize(s):
    """1 token per 4 chars — deterministic stand-in for llama-server /tokenize."""
    return len(s) // 4


# ── the reserve ──────────────────────────────────────────────────────────────

def test_reserve_is_counted_against_the_context_window():
    n_ctx, reserve = 1000, 300
    est, clamped = _estimate_and_clamp_tokens(
        "s" * 400, [_Msg("user", "u" * 400)], n_ctx, max_tokens=10_000,
        tokenize_fn=_fake_tokenize, reserve=reserve,
    )
    # 100 (system) + 100 (user) + 8*2 (template) + 300 (reserve)
    assert est == 516
    assert clamped == n_ctx - est
    # The whole point: what llama-server sees plus what it may generate must
    # fit. Pre-fix this summed to n_ctx + reserve and truncated mid-stream.
    assert est + clamped <= n_ctx


def test_prompt_plus_output_never_exceeds_n_ctx_when_clamp_binds():
    n_ctx = 2000
    est, clamped = _estimate_and_clamp_tokens(
        "s" * 4000, [_Msg("user", "u" * 2000)], n_ctx, max_tokens=100_000,
        tokenize_fn=_fake_tokenize, reserve=345,
    )
    assert clamped < 100_000, "clamp must bind for this to be a real test"
    assert est + clamped <= n_ctx


def test_reserve_applies_to_the_chars_fallback_path():
    def boom(_):
        raise RuntimeError("llama-server /tokenize down")

    est, clamped = _estimate_and_clamp_tokens(
        "s" * 250, [_Msg("user", "u" * 250)], 1000, max_tokens=10_000,
        tokenize_fn=boom, reserve=300,
    )
    assert est == int(500 / 2.5) + 300
    assert est + clamped <= 1000


def test_zero_reserve_matches_legacy_behaviour():
    est, clamped = _estimate_and_clamp_tokens(
        "s" * 400, [_Msg("user", "u" * 400)], 1000, max_tokens=10_000,
        tokenize_fn=_fake_tokenize, reserve=0,
    )
    assert est == 216
    assert clamped == 784


def test_empty_system_prompt_is_not_tokenized():
    """A dropped system prompt must contribute nothing — and not round-trip."""
    calls = []

    def counting(s):
        calls.append(s)
        return len(s) // 4

    est, _ = _estimate_and_clamp_tokens(
        "", [_Msg("system", "c" * 400)], 1000, max_tokens=100,
        tokenize_fn=counting, reserve=0,
    )
    assert "" not in calls
    assert est == 100 + 16


def test_clamp_floors_at_one_when_prompt_fills_the_window():
    est, clamped = _estimate_and_clamp_tokens(
        "s" * 8000, [_Msg("user", "u")], 500, max_tokens=4000,
        tokenize_fn=_fake_tokenize, reserve=345,
    )
    assert est > 500
    assert clamped == 1


# ── the guidance cost ────────────────────────────────────────────────────────

def test_guidance_cost_covers_the_real_guidance_string():
    """The reserve is measured with a worst-case number, so it can never be
    short of the guidance actually appended for any real clamped value."""
    reserve = _guidance_token_cost(_fake_tokenize)
    for available in (1, 940, 16_384, 524_288):
        actual = _fake_tokenize(
            "\n" + Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=available)
        )
        assert reserve >= actual


def test_guidance_cost_falls_back_when_tokenize_fails():
    def boom(_):
        raise RuntimeError("down")

    assert _guidance_token_cost(boom) > 0
    assert _guidance_token_cost(None) > 0
