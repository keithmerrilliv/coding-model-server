"""DEV-617: tokens spent + no visible content must 502, never empty-200.

The Qwen3.8 investigation (DEV-616) proved a reasoning model can burn
thousands of tokens inside an unclosed think block; post-strip the server
returned an empty 200 and callers recorded empty artifacts as answers —
the DEV-543 silent-success shape.
"""
import pytest
from fastapi import HTTPException

import coding_model_server.llama_server as ls


def guard(**kw):
    args = dict(text="", tool_calls=None,
                usage={"completion_tokens": 4677}, raw_text="<think>x", rid="t")
    args.update(kw)
    return ls._reject_reasoning_only_completion(
        args["text"], args["tool_calls"], args["usage"],
        args["raw_text"], args["rid"])


def test_reasoning_only_raises_502_naming_tokens():
    with pytest.raises(HTTPException) as e:
        guard()
    assert e.value.status_code == 502
    assert "4677" in e.value.detail
    assert "no visible content" in e.value.detail


def test_whitespace_only_content_also_raises():
    with pytest.raises(HTTPException):
        guard(text="  \n\t ")


def test_zero_tokens_passes_through():
    assert guard(usage={"completion_tokens": 0}) is None


def test_missing_usage_passes_through():
    assert guard(usage=None) is None


def test_tool_calls_with_empty_content_are_legitimate():
    # The supervisor's native decide() returns tool_calls and no content.
    assert guard(tool_calls=[{"function": {"name": "decide"}}]) is None


def test_real_content_passes_through():
    assert guard(text="Here is the design.") is None


def test_env_escape_hatch(monkeypatch):
    monkeypatch.setattr(ls, "ALLOW_EMPTY_COMPLETIONS", True)
    assert guard() is None
