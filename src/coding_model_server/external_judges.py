"""External-LLM call sites shared across the project.

Used by:
- ``coding_model_client.review``: ``/review`` fan-out (Claude + Gemini judges over
  an uncommitted git diff).
- ``coding_model_autonomous.executor``: adversarial test-writer (Gemini only,
  Phase b — fires after the local Coding Model reviewer's tests pass).

Each call function takes a system prompt + user content and returns the
model's text response, or raises ``RuntimeError`` on missing key, missing
SDK, or transport failure. SDK imports are lazy so the chat client and
orchestrator both start fine on systems where an optional SDK isn't
installed.

There are two Claude paths, differing only in how they authenticate:
- ``call_claude`` — the raw Anthropic Messages API, billed per token against
  ``ANTHROPIC_API_KEY``. Use where the key has real credit.
- ``call_claude_sdk`` — the Claude Agent SDK, which shells out to the local
  Claude Code CLI and bills against its **subscription**, not a per-token key.
  Use where there is a Claude Code login but no API credit (DEV-98). It
  deliberately unsets ``ANTHROPIC_API_KEY`` for the subprocess so an
  empty-credit key can't shadow the subscription and reproduce the billing
  400 that blocked DEV-90.

Timeout semantics: the Anthropic SDK accepts ``timeout`` natively. The
google-genai SDK (as currently pinned in requirements.txt) does not, so
``call_gemini`` enforces it via a ThreadPoolExecutor wrapper, and
``call_claude_sdk`` via ``asyncio.wait_for``. All functions therefore honour
the ``timeout`` argument identically from the caller's perspective.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError


# ── Defaults (callers may override via the model= kwarg) ─────────────────────

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_CLAUDE_SDK_MODEL = "claude-opus-4-8"
# Defaults to gemini-2.5-flash because pro models are paid-tier-only on the
# Gemini API (free tier limit=0 for gemini-3-pro-preview / gemini-2.5-pro).
# Override via REVIEW_GEMINI_MODEL or AUTONOMOUS_ADVERSARIAL_GEMINI_MODEL once
# paid billing is configured. gemini-3-pro-preview is the recommended upgrade.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


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


def call_claude_sdk(
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
    timeout: float,
    model: str = DEFAULT_CLAUDE_SDK_MODEL,
) -> str:
    """Claude via the Claude Agent SDK, authenticated through the local Claude
    Code subscription rather than a per-token API key (DEV-98).

    Single-turn and tool-free: system prompt + one user turn in, graded text
    out. ``allowed_tools=[]`` and ``max_turns=1`` keep it to one completion; a
    judge needs no agent loop, file access, or MCP.

    ``max_tokens`` is advisory — the Agent SDK exposes no hard output cap, and a
    judge's output (a short rationale + a VERDICT line) never approaches one.

    Forces the subscription: the SDK's subprocess inherits ``os.environ`` (it
    merges, not replaces), so an exported ``ANTHROPIC_API_KEY`` — including an
    empty-credit one sourced from ``.env`` — would be used instead of the Claude
    Code login and 400 on billing, exactly as in DEV-90. This removes the var
    for the duration of the call and restores it after. For an environment with
    real API credit and no subscription, use ``call_claude`` instead.
    """
    import asyncio

    try:
        import claude_agent_sdk as sdk
    except ImportError as e:
        raise RuntimeError(f"claude-agent-sdk not installed: {e}") from e

    options = sdk.ClaudeAgentOptions(
        system_prompt=system_prompt,   # a plain str replaces Claude Code's own prompt
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],            # don't load the cwd's CLAUDE.md / settings
        model=model,
    )

    async def _run() -> str:
        parts: list[str] = []
        error: str | None = None
        async for msg in sdk.query(prompt=user_content, options=options):
            if isinstance(msg, sdk.AssistantMessage):
                for block in msg.content:
                    if isinstance(block, sdk.TextBlock):
                        parts.append(block.text)
            elif isinstance(msg, sdk.ResultMessage) and msg.is_error:
                error = msg.api_error_status or msg.subtype or "unknown error"
        if error:
            raise RuntimeError(f"claude-agent-sdk judge errored: {error}")
        return "".join(parts).strip() or "(empty response)"

    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=timeout))
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"claude-agent-sdk judge timed out after {timeout:.0f}s") from e
    finally:
        if saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_key


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


def claude_sdk_available() -> tuple[bool, str | None]:
    """The Agent-SDK Claude path needs three things: the SDK, the Claude Code
    CLI it shells out to, and a subscription credential (or an OAuth token for
    headless use — the cron/CI caveat from DEV-98's ticket)."""
    import shutil

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, "`claude-agent-sdk` not installed (pip install claude-agent-sdk)"
    if not shutil.which("claude"):
        return False, "`claude` CLI not on PATH (the Agent SDK shells out to it)"
    creds = os.path.expanduser("~/.claude/.credentials.json")
    if not (os.path.exists(creds) or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")):
        return False, ("no Claude Code login found — run `claude` to sign in, or set "
                       "CLAUDE_CODE_OAUTH_TOKEN (via `claude setup-token`) for headless use")
    return True, None
