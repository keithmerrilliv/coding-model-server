"""Unit tests for the dynamic implementer output budget + truncation signal.

Covers the two halves of the #2 pipeline fix:
  - estimate_design_file_count / implementer_max_tokens_for: scale the
    implementer's max_tokens to the design's file count, clamped to a floor
    and ceiling (small specs unchanged, big specs get headroom).
  - call_agent's `meta` out-param: surfaces finish_reason / truncated so the
    daemon can emit OUTPUT_TRUNCATED instead of letting a cut-off file read as
    a phantom reviewer "missing file" FAIL.
"""
from coding_model_autonomous import executor


# ── file-count estimate ──────────────────────────────────────────────────────

def test_estimate_design_file_count_dedupes_and_filters():
    design = """
    ## File Structure
      ParamountDemo/package.json
      ParamountDemo/client/main.ts
      ParamountDemo/client/main.ts        # duplicate — counted once
      ParamountDemo/server/server.ts
      ParamountDemo/README.md
    Some prose mentioning a function name but no file path at all.
    """
    assert executor.estimate_design_file_count(design) == 4


def test_estimate_design_file_count_handles_empty():
    assert executor.estimate_design_file_count("") == 0
    assert executor.estimate_design_file_count(None) == 0


# ── budget clamp ─────────────────────────────────────────────────────────────

def _pin_budget_knobs(monkeypatch):
    monkeypatch.setattr(executor, "IMPLEMENTER_MAX_TOKENS", 16000)
    monkeypatch.setattr(executor, "IMPLEMENTER_MAX_TOKENS_CEILING", 48000)
    monkeypatch.setattr(executor, "IMPLEMENTER_TOKENS_PER_FILE", 1500)
    monkeypatch.setattr(executor, "IMPLEMENTER_TOKENS_BASE", 4000)


def test_small_design_clamps_to_floor(monkeypatch):
    _pin_budget_knobs(monkeypatch)
    # 1 file -> 4000 + 1500 = 5500, below the 16000 floor.
    assert executor.implementer_max_tokens_for("only.ts") == 16000


def test_midsize_design_scales_linearly(monkeypatch):
    _pin_budget_knobs(monkeypatch)
    design = "\n".join(f"dir/f{i}.ts" for i in range(21))  # the ParamountDemo size
    assert executor.implementer_max_tokens_for(design) == 4000 + 21 * 1500  # 35500


def test_huge_design_clamps_to_ceiling(monkeypatch):
    _pin_budget_knobs(monkeypatch)
    design = "\n".join(f"dir/f{i}.ts" for i in range(100))
    assert executor.implementer_max_tokens_for(design) == 48000


# ── call_agent truncation signal ─────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _patch_completion(monkeypatch, finish_reason, content="x"):
    payload = {"choices": [{"finish_reason": finish_reason,
                            "message": {"content": content}}]}
    monkeypatch.setattr(executor, "post_chat_completion",
                        lambda *a, **k: _FakeResp(payload))


def test_call_agent_meta_flags_length_as_truncated(monkeypatch):
    _patch_completion(monkeypatch, "length", content="partial file...")
    meta: dict = {}
    out = executor.call_agent("implementer", [{"role": "user", "content": "hi"}],
                              meta=meta)
    assert out == "partial file..."
    assert meta["truncated"] is True
    assert meta["finish_reason"] == "length"


def test_call_agent_meta_stop_is_not_truncated(monkeypatch):
    _patch_completion(monkeypatch, "stop", content="complete")
    meta: dict = {}
    executor.call_agent("reviewer", [{"role": "user", "content": "hi"}], meta=meta)
    assert meta["truncated"] is False
    assert meta["finish_reason"] == "stop"


def test_call_agent_without_meta_still_returns_content(monkeypatch):
    # Back-compat: callers that don't pass meta are unaffected.
    _patch_completion(monkeypatch, "stop", content="ok")
    assert executor.call_agent("architect", [{"role": "user", "content": "hi"}]) == "ok"
