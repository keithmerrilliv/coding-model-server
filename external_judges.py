"""External-LLM call sites shared across the project.

Used by:
- ``qwen_client.review``: ``/review`` fan-out (Claude + Gemini judges over
  an uncommitted git diff).
- ``qwen_autonomous.executor``: adversarial test-writer (Gemini only,
  Phase b — fires after the local Qwen reviewer's tests pass).

Each call function takes a system prompt + user content and returns the
model's text response, or raises ``RuntimeError`` on missing key, missing
SDK, or transport failure. SDK imports are lazy so the chat client and
orchestrator both start fine on systems where an optional SDK isn't
installed.

Timeout semantics: the Anthropic SDK accepts ``timeout`` natively. The
google-genai SDK (as currently pinned in requirements.txt) does not, so
``call_gemini`` enforces it via a ThreadPoolExecutor wrapper. Both
functions therefore honour the ``timeout`` argument identically from the
caller's perspective.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError


# ── Defaults (callers may override via the model= kwarg) ─────────────────────

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_GEMINI_MODEL = "gemini-3-pro"


# ── Call sites ───────────────────────────────────────────────────────────────

def call_claude(
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
    timeout: float,
    model: str = DEFAULT_CLAUDE_MODEL,
) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(f"anthropic SDK not installed: {e}") from e

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        timeout=timeout,
    )
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip() or "(empty response)"


def call_gemini(
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
    timeout: float,
    model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        raise RuntimeError(f"google-genai SDK not installed: {e}") from e

    def _do_call() -> str:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=user_content,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
            ),
        )
        return (resp.text or "").strip() or "(empty response)"

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_do_call)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeoutError as e:
            raise RuntimeError(f"Gemini call timed out after {timeout:.0f}s") from e


# ── Pre-flight helpers ───────────────────────────────────────────────────────

def claude_available() -> tuple[bool, str | None]:
    """Return (ok, reason_if_not). Used by pre-flight checks at startup."""
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return False, "ANTHROPIC_API_KEY not set"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "`anthropic` package not installed (pip install anthropic)"
    return True, None


def gemini_available() -> tuple[bool, str | None]:
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip():
        return False, "GEMINI_API_KEY (or GOOGLE_API_KEY) not set"
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False, "`google-genai` package not installed (pip install google-genai)"
    return True, None
