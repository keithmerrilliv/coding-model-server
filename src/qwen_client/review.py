"""Multi-judge `/review` fan-out for the chat client.

Sends the current uncommitted git diff to four reviewers in parallel —
Claude (subscription), Gemini (subscription), the local Qwen
``reviewer`` agent (Coder-30B-A3B), and the local Qwen ``deep_reviewer``
agent (Qwen3.5-122B-A10B) — and renders their verdicts as sequential
vertical blocks with colored headers. The human reconciles; there is
no synthesizer.

Fail-open everywhere: if any judge is unconfigured, errors out, or
times out, the others still run and the failure shows as that judge's
block. Pre-flight check happens up front so the user knows whether
they'll see two or four columns before waiting.

The two Qwen judges share the local server's sequential inference
lock and run back-to-back in a single worker thread, while the
external Claude and Gemini calls run in parallel against them. End-to-
end wall-clock is roughly max(Claude, Gemini, reviewer + deep_reviewer).

Hard deps for the external judges live in requirements.txt
(`anthropic`, `google-genai`); imports are lazy so the chat client
keeps starting on systems where they're missing.
"""
from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Optional

from qwen_server import external_judges
from qwen_client.config import COLORS, print_colored
from qwen_client.http import post_chat_completion


# ── Configuration (env-overridable) ──────────────────────────────────────────

CLAUDE_MODEL = os.getenv("REVIEW_CLAUDE_MODEL", "claude-sonnet-4-6")
GEMINI_MODEL = os.getenv("REVIEW_GEMINI_MODEL", "gemini-3-pro")
QWEN_REVIEWER_AGENT = os.getenv("REVIEW_QWEN_REVIEWER_AGENT", "reviewer")
QWEN_DEEP_REVIEWER_AGENT = os.getenv("REVIEW_QWEN_DEEP_REVIEWER_AGENT", "deep_reviewer")

REVIEW_TIMEOUT = float(os.getenv("REVIEW_TIMEOUT", "120"))
REVIEW_MAX_TOKENS = int(os.getenv("REVIEW_MAX_TOKENS", "2000"))
DIFF_MAX_BYTES = int(os.getenv("REVIEW_DIFF_MAX_BYTES", str(256 * 1024)))


# ── Shared system prompt ─────────────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """\
You are a code reviewer evaluating a git diff. Be terse and concrete.

Identify in this order:
1. BUGS — logic errors, off-by-one, null derefs, race conditions, resource leaks.
2. SECURITY — injection, secrets in code, unsafe deserialization, missing auth checks, weak crypto.
3. CORRECTNESS — does the diff do what the surrounding code/comments imply?
4. STYLE — only flag if it materially hurts maintainability. No formatting nits.

Each finding: one bullet, cite path:line where possible. If you find nothing serious, say so plainly.

Format strictly as markdown:

## Bugs
- ...

## Security
- ...

## Correctness
- ...

## Style
- ...

Skip any heading whose section is empty. End with exactly one line:

VERDICT: ok | concerns | blocking
"""


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class JudgeResult:
    name: str           # display name, e.g. "Claude (sonnet-4-6)"
    color: str          # ANSI color code from COLORS dict
    body: str           # rendered text — review markdown OR error string
    elapsed_s: float    # wall-clock duration
    ok: bool            # True if a real review came back


# ── Diff capture ─────────────────────────────────────────────────────────────

def get_uncommitted_diff(cwd: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (diff_text, error). One of them is always None.

    Includes both staged and unstaged changes. Untracked files are NOT
    included — `git diff` doesn't show them and adding them via
    --no-index would inflate the diff with binary noise.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return None, "`git diff HEAD` timed out after 10s"

    if result.returncode != 0:
        # Common case: not a git repo. stderr has the real reason.
        return None, (result.stderr or "git diff failed").strip()

    diff = result.stdout
    if not diff.strip():
        return None, "no uncommitted changes (working tree matches HEAD)"

    if len(diff) > DIFF_MAX_BYTES:
        head = diff[: DIFF_MAX_BYTES // 2]
        tail = diff[-DIFF_MAX_BYTES // 2 :]
        diff = (
            head
            + f"\n\n... [truncated {len(diff) - DIFF_MAX_BYTES} bytes — "
              f"diff exceeds REVIEW_DIFF_MAX_BYTES={DIFF_MAX_BYTES}] ...\n\n"
            + tail
        )
    return diff, None


# ── Per-judge call sites (each returns a markdown body or raises) ────────────
#
# Claude/Gemini calls live in the shared ``external_judges`` module so the
# autonomous orchestrator can reuse them (Phase b adversarial test-writing).
# The local Qwen call stays here because it's review-specific (talks to the
# local server's OpenAI-compatible endpoint, not an external SDK).

def _call_qwen(diff: str, agent: str) -> str:
    """Call a local-server Qwen agent. Used for both reviewer and deep_reviewer."""
    resp = post_chat_completion(
        agent,
        [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": diff},
        ],
        timeout=REVIEW_TIMEOUT,
        max_tokens=REVIEW_MAX_TOKENS,
        temperature=0.2,
        skip_memory=True,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"server response missing choices/content: {e}")
    return content.strip() or "(empty response)"


# ── Fan-out orchestration ────────────────────────────────────────────────────

def fan_out_review(diff: str) -> list[JudgeResult]:
    """Fire all four judges in parallel, gather results, return in display order.

    External judges (Claude, Gemini) get one thread each. The two local
    Qwen judges share a single worker thread because the inference lock
    serializes them anyway.
    """
    results: dict[str, JudgeResult] = {}

    def _wrap(name: str, color: str, fn) -> JudgeResult:
        t0 = time.monotonic()
        try:
            body = fn()
            return JudgeResult(name=name, color=color, body=body,
                               elapsed_s=time.monotonic() - t0, ok=True)
        except Exception as e:  # noqa: BLE001
            return JudgeResult(
                name=name, color=color,
                body=f"(failed: {type(e).__name__}: {e})",
                elapsed_s=time.monotonic() - t0, ok=False,
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_wrap, f"Claude ({CLAUDE_MODEL})", COLORS['CYAN'],
                        lambda: external_judges.call_claude(
                            REVIEW_SYSTEM_PROMPT, diff,
                            max_tokens=REVIEW_MAX_TOKENS,
                            timeout=REVIEW_TIMEOUT,
                            model=CLAUDE_MODEL,
                        )): "claude",
            pool.submit(_wrap, f"Gemini ({GEMINI_MODEL})", COLORS['BLUE'],
                        lambda: external_judges.call_gemini(
                            REVIEW_SYSTEM_PROMPT, diff,
                            max_tokens=REVIEW_MAX_TOKENS,
                            timeout=REVIEW_TIMEOUT,
                            model=GEMINI_MODEL,
                        )): "gemini",
            pool.submit(_wrap_qwen_pair, diff): "qwen_pair",
        }
        for fut in futures:
            tag = futures[fut]
            try:
                if tag == "qwen_pair":
                    rev, deep = fut.result(timeout=REVIEW_TIMEOUT * 2 + 5)
                    results["reviewer"] = rev
                    results["deep_reviewer"] = deep
                else:
                    results[tag] = fut.result(timeout=REVIEW_TIMEOUT + 5)
            except FuturesTimeoutError:
                if tag == "qwen_pair":
                    msg = "(timed out waiting for both Qwen judges)"
                    results["reviewer"] = JudgeResult(
                        f"Qwen reviewer ({QWEN_REVIEWER_AGENT})", COLORS['GREEN'],
                        msg, REVIEW_TIMEOUT * 2, False,
                    )
                    results["deep_reviewer"] = JudgeResult(
                        f"Qwen deep_reviewer ({QWEN_DEEP_REVIEWER_AGENT})", COLORS['HEADER'],
                        msg, REVIEW_TIMEOUT * 2, False,
                    )
                else:
                    color = COLORS['CYAN'] if tag == "claude" else COLORS['BLUE']
                    name = f"Claude ({CLAUDE_MODEL})" if tag == "claude" else f"Gemini ({GEMINI_MODEL})"
                    results[tag] = JudgeResult(
                        name, color, "(timed out)", REVIEW_TIMEOUT, False,
                    )

    return [
        results["claude"],
        results["gemini"],
        results["reviewer"],
        results["deep_reviewer"],
    ]


def _wrap_qwen_pair(diff: str) -> tuple[JudgeResult, JudgeResult]:
    """Run reviewer then deep_reviewer in one thread; package as two JudgeResults."""
    name_rev = f"Qwen reviewer ({QWEN_REVIEWER_AGENT})"
    name_deep = f"Qwen deep_reviewer ({QWEN_DEEP_REVIEWER_AGENT})"

    t0 = time.monotonic()
    try:
        rev_body = _call_qwen(diff, QWEN_REVIEWER_AGENT)
        rev_result = JudgeResult(name_rev, COLORS['GREEN'], rev_body,
                                 time.monotonic() - t0, True)
    except Exception as e:  # noqa: BLE001
        rev_result = JudgeResult(
            name_rev, COLORS['GREEN'],
            f"(failed: {type(e).__name__}: {e})",
            time.monotonic() - t0, False,
        )

    t0 = time.monotonic()
    try:
        deep_body = _call_qwen(diff, QWEN_DEEP_REVIEWER_AGENT)
        deep_result = JudgeResult(name_deep, COLORS['HEADER'], deep_body,
                                  time.monotonic() - t0, True)
    except Exception as e:  # noqa: BLE001
        deep_result = JudgeResult(
            name_deep, COLORS['HEADER'],
            f"(failed: {type(e).__name__}: {e})",
            time.monotonic() - t0, False,
        )

    return rev_result, deep_result


# ── Rendering ────────────────────────────────────────────────────────────────

def render_reviews(results: list[JudgeResult]) -> None:
    sep = "=" * 72
    print()
    for r in results:
        print_colored(sep, r.color)
        print_colored(f"  {r.name}    [{r.elapsed_s:.1f}s]", r.color)
        print_colored(sep, r.color)
        print(r.body.rstrip())
        print()

    # Quick verdict-line digest at the bottom.
    print_colored("Verdict digest:", COLORS['BOLD'])
    for r in results:
        verdict = _extract_verdict_line(r.body) if r.ok else r.body.splitlines()[0]
        print_colored(f"  {r.name}: {verdict}", r.color)
    print()


def _extract_verdict_line(body: str) -> str:
    for line in reversed(body.splitlines()):
        if line.strip().upper().startswith("VERDICT"):
            return line.strip()
    return "(no VERDICT line — see body)"


# ── Pre-flight + entry point ─────────────────────────────────────────────────

def _pre_flight() -> list[str]:
    """Return human-readable warnings for any judge that won't run.

    Doesn't block — fail-open is the policy. Just reports up front so
    the user isn't surprised by missing columns.
    """
    warnings: list[str] = []
    ok, reason = external_judges.claude_available()
    if not ok:
        warnings.append(f"{reason} — Claude judge will be skipped.")
    ok, reason = external_judges.gemini_available()
    if not ok:
        warnings.append(f"{reason} — Gemini judge will be skipped.")
    return warnings


def run_review_command() -> None:
    """Entry point called from commands.py for `/review` (no args)."""
    diff, err = get_uncommitted_diff()
    if err is not None:
        print_colored(f"/review: {err}", COLORS['WARNING'])
        return

    for w in _pre_flight():
        print_colored(f"  note: {w}", COLORS['WARNING'])

    print_colored(
        f"\nFanning out {len(diff)}-byte diff to 4 judges "
        f"(timeout={REVIEW_TIMEOUT:.0f}s each)…",
        COLORS['BOLD'],
    )
    t0 = time.monotonic()
    results = fan_out_review(diff)
    print_colored(f"All judges returned in {time.monotonic() - t0:.1f}s.", COLORS['BOLD'])
    render_reviews(results)
