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
from coding_model_server.llama_server import LlamaServerManager
from coding_model_server.routes.chat import (
    MIN_COMPLETION_TOKENS,
    PromptTooLargeError,
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


def test_rag_suffix_is_counted_but_tokenized_separately():
    """DEV-113: the RAG block contributes to the estimate, but as its own
    tokenize call — never concatenated onto the base prompt (which would evict
    the base's cache entry every retrieval)."""
    calls = []

    def counting(s):
        calls.append(s)
        return len(s) // 4

    base = "s" * 400
    suffix = "r" * 200
    est, _ = _estimate_and_clamp_tokens(
        base, [_Msg("user", "u" * 400)], 100_000, max_tokens=1000,
        tokenize_fn=counting, reserve=0, rag_suffix=suffix,
    )
    # base + suffix + user tokenized as three distinct strings.
    assert base in calls and suffix in calls
    assert base + suffix not in calls
    # 100 (base) + 50 (suffix) + 100 (user) + 8*2 (template)
    assert est == 100 + 50 + 100 + 16


def test_rag_suffix_counted_in_the_chars_fallback():
    def boom(_):
        raise RuntimeError("tokenize down")

    est, _ = _estimate_and_clamp_tokens(
        "s" * 250, [_Msg("user", "u" * 250)], 100_000, max_tokens=1000,
        tokenize_fn=boom, reserve=0, rag_suffix="r" * 500,
    )
    # (250 + 500 + 250) / 2.5
    assert est == int(1000 / 2.5)


# ── DEV-195: overflow is a 413, not a silently useless 1-token budget ────────

def test_prompt_filling_the_window_raises_instead_of_a_1_token_stub():
    # Pre-fix this clamped to 1: generation "succeeded", the caller got a
    # truncated stub, the parser failed, and a retry rotation burned on a
    # request that could never work.
    with pytest.raises(PromptTooLargeError, match="exceed context window"):
        _estimate_and_clamp_tokens(
            "s" * 8000, [_Msg("user", "u")], 500, max_tokens=4000,
            tokenize_fn=_fake_tokenize, reserve=345,
        )


def test_overflow_raises_on_the_chars_fallback_path_too():
    def boom(_):
        raise RuntimeError("tokenize down")

    with pytest.raises(PromptTooLargeError):
        _estimate_and_clamp_tokens(
            "s" * 8000, [_Msg("user", "u")], 500, max_tokens=4000,
            tokenize_fn=boom, reserve=345,
        )


def test_caller_asking_below_the_floor_is_served_if_it_fits():
    # est = 970 + 16 template = 986; available = 14 < MIN_COMPLETION_TOKENS,
    # but the caller only asked for 8 — deliverable, so no 413.
    est, clamped = _estimate_and_clamp_tokens(
        "s" * 3880, [_Msg("user", "u")], 1000, max_tokens=8,
        tokenize_fn=_fake_tokenize, reserve=0,
    )
    assert est + clamped <= 1000
    assert clamped == 8


def test_floor_matches_the_documented_minimum():
    # available lands exactly one token under the floor → rejected; at the
    # floor → served. Pins the boundary so the env knob stays meaningful.
    n_ctx = 1000
    for available, should_raise in ((MIN_COMPLETION_TOKENS - 1, True),
                                    (MIN_COMPLETION_TOKENS, False)):
        prompt_tokens = n_ctx - available - 16  # 16 = template overhead
        args = ("s" * (prompt_tokens * 4), [_Msg("user", "u")], n_ctx)
        if should_raise:
            with pytest.raises(PromptTooLargeError):
                _estimate_and_clamp_tokens(*args, max_tokens=4000,
                                           tokenize_fn=_fake_tokenize, reserve=0)
        else:
            _, clamped = _estimate_and_clamp_tokens(
                *args, max_tokens=4000, tokenize_fn=_fake_tokenize, reserve=0)
            assert clamped == available


# ── the guidance cost ────────────────────────────────────────────────────────

@pytest.mark.parametrize("guidance", [
    Config.TOKEN_BUDGET_GUIDANCE,
    Config.TOKEN_BUDGET_GUIDANCE_CORE,
])
def test_guidance_cost_covers_the_real_guidance_string(guidance):
    """The reserve is measured with a worst-case number, so it can never be
    short of the guidance actually appended for any real clamped value."""
    reserve = _guidance_token_cost(guidance, _fake_tokenize)
    for available in (1, 940, 16_384, 524_288):
        actual = _fake_tokenize(
            "\n" + guidance.format(available_tokens=available)
        )
        assert reserve >= actual


def test_guidance_cost_falls_back_when_tokenize_fails():
    def boom(_):
        raise RuntimeError("down")

    assert _guidance_token_cost(Config.TOKEN_BUDGET_GUIDANCE, boom) > 0
    assert _guidance_token_cost(Config.TOKEN_BUDGET_GUIDANCE, None) > 0


# ── DEV-79: the guidance must reach programmatic callers, and be safe ────────
#
# _build_openai_messages drops the agent system prompt when the caller sends
# its own — which every autonomous agent does. The budget guidance rode that
# prompt, so it never reached them. It is now folded into the caller's own
# system message instead, using the CORE variant: the interactive variant tells
# the model to emit <<<CONTINUE>>> (nothing handles it) and to assemble files
# with shell tools (it has none), both of which would *cause* the truncated
# file sets this machinery exists to prevent.

def test_core_guidance_omits_the_unhandled_continuation_protocol():
    core = Config.TOKEN_BUDGET_GUIDANCE_CORE.format(available_tokens=8000)
    assert "<<<CONTINUE>>>" not in core
    assert "REMAINING:" not in core
    # ...and it says so explicitly, so the model doesn't invent one. Compare
    # on unwrapped text — the prompt is hard-wrapped, so the phrase spans lines.
    assert "no continuation turn" in " ".join(core.split())


def test_core_guidance_omits_tool_advice_the_pipeline_cannot_follow():
    """The autonomous implementer has no tools — it emits <<<FILE:>>> blocks a
    regex parses. Telling it to assemble files with `cat` yields unparseable
    output."""
    core = Config.TOKEN_BUDGET_GUIDANCE_CORE.format(available_tokens=8000)
    assert "shell tools" not in core
    assert "incremental replace" not in core
    assert "temporary files" not in core


def test_core_guidance_never_licenses_dropping_required_output():
    """'Prioritise critical files over auxiliary ones' invites the implementer
    to omit files, which the reviewer then reports as a missing-file FAIL."""
    core = Config.TOKEN_BUDGET_GUIDANCE_CORE.format(available_tokens=8000)
    assert "Critical files before auxiliary ones" not in core
    assert "NOT drop, stub, or partially emit" in core


def test_core_guidance_still_carries_the_budget_figure():
    core = Config.TOKEN_BUDGET_GUIDANCE_CORE.format(available_tokens=8000)
    assert "OUTPUT BUDGET: ~8000 tokens" in core


def test_core_guidance_costs_less_than_the_interactive_variant():
    """It drops two sections, so a caller reserving CORE must not be charged
    the interactive price (that was the second bug in commit 65439367)."""
    assert (_guidance_token_cost(Config.TOKEN_BUDGET_GUIDANCE_CORE, _fake_tokenize)
            < _guidance_token_cost(Config.TOKEN_BUDGET_GUIDANCE, _fake_tokenize))


def test_folded_guidance_survives_the_drop_and_reaches_the_wire():
    """The bug, at the exact site that caused it.

    _build_openai_messages drops the agent system prompt for a caller with its
    own system message. Guidance ridden in on that prompt is dropped with it —
    that is why no autonomous agent ever saw one. Folded into the caller's own
    message, it survives; and there is still exactly ONE system message, so the
    strict Jinja templates don't reject the request.
    """
    clamped = 8000
    caller_system = _Msg(
        "system",
        "You are the implementer. Emit <<<FILE: path>>> … <<<END_FILE>>>.",
    )
    messages = [caller_system, _Msg("user", "Build the app.")]

    # What routes/chat.py does when client_has_system.
    caller_system.content += "\n" + Config.TOKEN_BUDGET_GUIDANCE_CORE.format(
        available_tokens=clamped
    )

    built = LlamaServerManager._build_openai_messages(
        messages, system_prompt="AGENT SYSTEM PROMPT (dropped for this caller)"
    )

    systems = [m for m in built if m["role"] == "system"]
    assert len(systems) == 1, "a second system message would be rejected by the template"
    assert "AGENT SYSTEM PROMPT" not in systems[0]["content"], "still dropped, as designed"
    # The payload that never used to arrive:
    assert f"OUTPUT BUDGET: ~{clamped} tokens" in systems[0]["content"]
    assert "NEVER TRUNCATE A UNIT" in systems[0]["content"]
    # The caller's own task prompt still leads, so its markers keep their place.
    assert systems[0]["content"].startswith("You are the implementer.")
    assert "<<<CONTINUE>>>" not in systems[0]["content"]


def test_interactive_caller_still_gets_the_full_guidance_on_the_agent_prompt():
    """The other population must be unaffected: no client system message, so the
    agent prompt (with the full guidance appended) still ships."""
    messages = [_Msg("user", "hello")]
    augmented = "AGENT PROMPT\n" + Config.TOKEN_BUDGET_GUIDANCE.format(
        available_tokens=4096
    )
    built = LlamaServerManager._build_openai_messages(messages, system_prompt=augmented)

    systems = [m for m in built if m["role"] == "system"]
    assert len(systems) == 1
    assert "AGENT PROMPT" in systems[0]["content"]
    assert "OUTPUT BUDGET: ~4096 tokens" in systems[0]["content"]
    assert "<<<CONTINUE>>>" in systems[0]["content"], "interactive keeps its protocol"


def test_folding_core_into_the_caller_system_message_still_fits_n_ctx():
    """The regression that commit 65439367 fixed, in the branch that now ships
    guidance for the first time: reserve it, or prompt+output overruns n_ctx
    and llama-server (context-shift disabled) truncates mid-symbol."""
    n_ctx = 4000
    caller_system = _Msg("system", "s" * 2000)
    messages = [caller_system, _Msg("user", "u" * 2000)]

    reserve = _guidance_token_cost(Config.TOKEN_BUDGET_GUIDANCE_CORE, _fake_tokenize)
    est, clamped = _estimate_and_clamp_tokens(
        "", messages, n_ctx, max_tokens=100_000,   # agent prompt dropped ⇒ ""
        tokenize_fn=_fake_tokenize, reserve=reserve,
    )
    assert clamped < 100_000, "clamp must bind for this to be a real test"

    # Now do what the route does: fold the guidance in, and re-measure the real
    # on-the-wire prompt. It must still fit alongside the granted output.
    caller_system.content += "\n" + Config.TOKEN_BUDGET_GUIDANCE_CORE.format(
        available_tokens=clamped
    )
    on_the_wire = sum(_fake_tokenize(m.content) for m in messages) + 8 * (len(messages) + 1)
    assert on_the_wire + clamped <= n_ctx


def test_route_converts_overflow_to_413(monkeypatch):
    import asyncio
    from unittest import mock

    from fastapi import HTTPException

    from coding_model_server.routes import chat
    from coding_model_server.schemas import ChatCompletionRequest, ChatMessage

    fake_mgr = mock.Mock()
    fake_mgr.tokenize.side_effect = lambda t: 10_000_000  # dwarfs any n_ctx
    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    monkeypatch.setattr(chat.runtime.services, "memory", None, raising=False)

    class _State:
        pass

    class _Req:
        def __init__(self):
            self.state = _State()

    raw = _Req()
    request = ChatCompletionRequest(
        model="implementer",
        messages=[ChatMessage(role="user", content="hi")],
        stream=False,
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(chat.chat_completions(request, raw))
    assert exc_info.value.status_code == 413
    # The client's _is_context_error trims history and retries on this phrase.
    assert "exceed context window" in exc_info.value.detail
    assert raw.state.error_category == "4xx_prompt_too_large"


# ── DEV-409: the advertised figure must be prefix-cache-stable ────────────

from coding_model_server.routes.chat import _stable_budget_figure


def test_nearby_clamps_share_a_bucket():
    """Consecutive agent calls differ by a few hundred prompt tokens; the
    interpolated figure — and therefore the system-prompt bytes — must not
    change with them, or llama-server's prefix cache dies at that token."""
    assert _stable_budget_figure(50_000) == _stable_budget_figure(51_000)


def test_figure_never_exceeds_the_real_clamp():
    for clamp in (300, 511, 512, 8191, 8192, 50_000, 131_072):
        assert _stable_budget_figure(clamp) <= clamp


def test_small_budgets_pass_through_exactly():
    # Near the MIN_COMPLETION_TOKENS floor there is no room to round away.
    assert _stable_budget_figure(300) == 300


def test_bucket_granularity():
    assert _stable_budget_figure(50_000) == 49_152      # 4096-grid
    assert _stable_budget_figure(8_191) == 7_680        # 512-grid below 8192
    assert _stable_budget_figure(8_192) == 8_192


def test_guidance_text_is_byte_identical_across_similar_calls():
    for guidance in (Config.TOKEN_BUDGET_GUIDANCE,
                     Config.TOKEN_BUDGET_GUIDANCE_CORE):
        a = guidance.format(available_tokens=_stable_budget_figure(50_000))
        b = guidance.format(available_tokens=_stable_budget_figure(51_000))
        assert a == b
