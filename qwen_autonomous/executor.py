"""Execution engine for autonomous mode (Phase 2).

Drives specs through architect → implementer → reviewer, calling each
agent via the qwen-server inference API, parsing structured responses,
writing artifacts to the spec workspace, running tests, and managing
the review-gate / retry loop.

The daemon calls into this module from its ``_process_executing`` handler.
Every function here is *synchronous* — it blocks the daemon's tick thread
for the full duration of an inference call. That's intentional: the
hardware only has one GPU and a sequential inference lock, so there's
nothing else the daemon could usefully do in parallel.

Response formats use ``<<<MARKER>>>…<<<END>>>``-style delimiters that
match the pattern established by the planner. The models' chat templates
are compatible with these markers — they treat them as user-defined
structured output, not as built-in tool calls.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("orchestrator.executor")


# ── Configuration ────────────────────────────────────────────────────────────

ARCHITECT_AGENT = os.getenv("AUTONOMOUS_ARCHITECT_AGENT", "q36_architect")
IMPLEMENTER_AGENT = os.getenv("AUTONOMOUS_IMPLEMENTER_AGENT", "implementer")
REVIEWER_AGENT = os.getenv("AUTONOMOUS_REVIEWER_AGENT", "reviewer")

ARCHITECT_TIMEOUT = float(os.getenv("AUTONOMOUS_ARCHITECT_TIMEOUT", "2700"))
IMPLEMENTER_TIMEOUT = float(os.getenv("AUTONOMOUS_IMPLEMENTER_TIMEOUT", "1800"))
REVIEWER_TIMEOUT = float(os.getenv("AUTONOMOUS_REVIEWER_TIMEOUT", "2700"))

ARCHITECT_MAX_TOKENS = int(os.getenv("AUTONOMOUS_ARCHITECT_MAX_TOKENS", "8000"))
IMPLEMENTER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_IMPLEMENTER_MAX_TOKENS", "16000"))
REVIEWER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_REVIEWER_MAX_TOKENS", "16000"))

MAX_RETRIES = int(os.getenv("AUTONOMOUS_MAX_RETRIES", "3"))

QWEN_SERVER_HOST = os.getenv("QWEN_SERVER_IP", "127.0.0.1")
QWEN_SERVER_PORT = int(os.getenv("QWEN_SERVER_PORT", "5000"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

ROLE_TO_AGENT = {
    "architect": ARCHITECT_AGENT,
    "implementer": IMPLEMENTER_AGENT,
    "reviewer": REVIEWER_AGENT,
}
ROLE_TO_TIMEOUT = {
    "architect": ARCHITECT_TIMEOUT,
    "implementer": IMPLEMENTER_TIMEOUT,
    "reviewer": REVIEWER_TIMEOUT,
}
ROLE_TO_MAX_TOKENS = {
    "architect": ARCHITECT_MAX_TOKENS,
    "implementer": IMPLEMENTER_MAX_TOKENS,
    "reviewer": REVIEWER_MAX_TOKENS,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Remove ``<think>…</think>`` blocks leaked by some Qwen3.5 variants."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _write_artifact(spec_dir: Path, rel_path: str, content: str) -> Path:
    """Write a file to the spec workspace with path-traversal protection.

    Strips leading ``/`` so models can't write absolute paths (Python's
    ``Path / "/abs"`` would discard the left operand). Then resolves
    symlinks and checks the result is still under ``spec_dir``.

    Uses Path.is_relative_to (not str.startswith): the latter would treat
    /work/abc-evil/x as nested under /work/abc, so a sibling-prefix dir
    escape would not be caught. is_relative_to compares path components.
    """
    # Strip leading slashes only — do NOT use lstrip("./") which eats
    # individual chars and would normalize "../../../x" into "x".
    while rel_path.startswith("/"):
        rel_path = rel_path[1:]
    spec_root = spec_dir.resolve()
    abs_path = (spec_dir / rel_path).resolve()
    if not abs_path.is_relative_to(spec_root):
        raise ValueError(f"Path traversal rejected: {rel_path}")
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content)
    return abs_path


def role_to_agent(role: str) -> str:
    return ROLE_TO_AGENT.get(role, IMPLEMENTER_AGENT)


# ── Complexity → implementer tier mapping ────────────────────────────────────
#
# The architect emits a COMPLEXITY block (parsed by parse_architect_response).
# `_select_implementer_agent` consults the architect's recommendation if it's
# in the whitelist; otherwise it falls back to the tier default; otherwise
# the env-default IMPLEMENTER_AGENT. Telemetry is deferred — see
# ~/.claude/projects/.../memory/project_implementer_telemetry.md.

TIER_TO_IMPLEMENTER = {
    "low": "fast_implementer",
    "medium": "implementer",
    "high": "deep_implementer",
    "extreme": "m25_implementer",
}

ALLOWED_IMPLEMENTER_AGENTS = {
    "fast_implementer", "implementer", "deep_implementer", "m25_implementer", "glm",
}


# ── System prompts ───────────────────────────────────────────────────────────

ARCHITECT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the ARCHITECT agent for an autonomous software development service.
    Your job: read the specification and produce a design document that the
    IMPLEMENTER can execute exactly, without ambiguity. You also assess the
    project's complexity and recommend which implementer agent should build it.

    # Output format

    Respond with EXACTLY TWO blocks, in this order. No preamble. No text
    outside the markers.

    <<<DESIGN>>>
    # Architecture: <project title>

    ## Overview
    <2-3 sentences summarizing what this project is and the key design decisions>

    ## Components
    <list each component/module with its responsibility>

    ## File Structure
    <tree showing every file to be created, with a one-line purpose each>

    ## Data Models
    <key types, schemas, or data structures>

    ## Implementation Notes
    <constraints the implementer must follow, edge cases to handle, etc.>

    ## Acceptance Criteria Checklist
    - [ ] <each criterion from the spec, restated in testable form>
    <<<END>>>

    <<<COMPLEXITY>>>
    Tier: <low|medium|high|extreme>
    Recommended agent: <fast_implementer|implementer|deep_implementer|m25_implementer|glm>
    Justification: <one or two sentences citing concrete signals — file count,
    language count, algorithmic depth, integration surface, edge-case density,
    test surface — that drove the tier and agent choice>
    <<<END_COMPLEXITY>>>

    # Tier guide

    - low:     1-2 small files, single language, no external integrations,
               trivial logic, < 10 test cases. Suits `fast_implementer`.
    - medium:  3-6 files, single primary language, modest algorithmic content,
               10-30 test cases. Suits `implementer` (the default).
    - high:    7+ files OR cross-language OR non-trivial algorithms (parsers,
               schedulers, custom data structures) OR external integrations
               (HTTP clients, DB schemas) OR 30+ test cases. Suits
               `deep_implementer`.
    - extreme: production system with concurrency, persistence, or security
               surface; multi-module refactor of an existing codebase; or
               anything where correctness depends on subtle invariants.
               Suits `m25_implementer`.

    # Rules

    1. Be prescriptive. The implementer follows your design exactly.
    2. Specify exact file paths relative to the project workspace root.
    3. Keep it concise — the implementer needs clarity, not prose.
    4. Do NOT write implementation code. That is the implementer's job.
    5. Do NOT add scope beyond what the spec requests.
    6. Every acceptance criterion from the spec must appear in your checklist.
    7. Pick the LOWEST tier that fits — do not over-allocate compute. A `high`
       agent for a `low` job wastes time and VRAM; the inverse causes failures.
    """)

IMPLEMENTER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the IMPLEMENTER agent for an autonomous software development service.
    You receive a specification and an architecture design, and you produce
    working code files.

    # Output format

    Respond with one or more file blocks. Every file you create MUST appear
    in its own block. The daemon only reads these blocks — prose outside
    them is ignored for file extraction.

    <<<FILE: relative/path/to/file.py>>>
    <complete file content — NOT a diff, the ENTIRE file>
    <<<END_FILE>>>

    <<<FILE: another/file.py>>>
    <complete file content>
    <<<END_FILE>>>

    # Rules

    1. EVERY file must appear in a <<<FILE: path>>>…<<<END_FILE>>> block.
    2. Paths are relative to the project workspace root (no leading /).
    3. Implement ALL components from the design. Do not skip "trivial" parts.
    4. Do NOT create test files — the REVIEWER handles those.
    5. If the spec requires a requirements.txt, setup.py, or similar, include it.
    6. Write COMPLETE files, not snippets or diffs.
    7. Inside a <<<FILE>>> block, write the file's RAW content. Do NOT wrap
       the content in markdown ```language fences — the daemon strips them
       defensively, but a missing close fence (e.g. on truncation) corrupts
       the file. Just emit the content directly.
    8. On a retry attempt (when previous code was rejected), you will see
       the reviewer's feedback. Fix every issue identified and output ALL
       files again — the daemon overwrites previous versions.
    """)

REVIEWER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the REVIEWER for an autonomous software service. You receive the
    spec, the design, and the implementer's source files. You (1) write test
    files that exercise the acceptance criteria, and (2) review the code.

    Your verdict reflects STATIC CODE REVIEW only — you do not see test
    execution output, and the orchestrator runs your tests separately. Phrase
    findings as "test X is designed to check Y", never "tests passed".

    # Output format

    Test files first (one block per file):

        <<<FILE: test_something.py>>>
        <complete test file content>
        <<<END_FILE>>>

    Then exactly one review block:

        <<<REVIEW>>>
        ## Test Files Written
        - <file>: <which acceptance criteria it exercises>

        ## Code Review
        ### Issues Found
        - <severity: critical/major/minor> <file>:<line> — <description>
        (Or: "No issues found.")

        ### Verdict
        PASS
        (Or FAIL if you found a critical code defect.)

        ### Verdict Evidence
        <REQUIRED — see below>

        ### Notes
        <anything the implementer should know on retry>
        <<<END_REVIEW>>>

    # Verdict Evidence (REQUIRED, parsed by the orchestrator)

    Empty or missing evidence forces FAIL — an unanchored verdict is rejected.

    For PASS: one line per acceptance criterion mapped to its test:
        - <criterion text> → <test_file.py::test_function_name>

    For FAIL: one line per blocking defect:
        - <severity> <file>:<line> — <description>

    No prose in this field. No test-result claims.

    # Rules

    1. Use the framework named in the plan (default: pytest).
    2. Tests must be runnable: real imports, real paths, no spec-violating mocks.
    3. PASS requires no critical/major defects you can cite by file:line.
    4. Do not expand scope beyond the spec.
    """)


# ── Agent calling ────────────────────────────────────────────────────────────

def call_agent(
    role: str,
    messages: list[dict[str, str]],
    *,
    agent: str | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> str:
    """Call an agent via the qwen-server inference API.

    Returns the raw content string from the model's response.
    Raises on transport or HTTP errors so the caller can decide
    whether to retry or fail.
    """
    agent = agent or role_to_agent(role)
    max_tokens = max_tokens or ROLE_TO_MAX_TOKENS.get(role, 8000)
    timeout = timeout or ROLE_TO_TIMEOUT.get(role, 1800)

    url = f"http://{QWEN_SERVER_HOST}:{QWEN_SERVER_PORT}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if ADMIN_API_KEY:
        headers["X-Admin-Key"] = ADMIN_API_KEY

    payload = {
        "model": agent,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
        # Opt out of server-side RAG: chat-memory injection is noise for
        # autonomous structured prompts (specs, designs, code blocks).
        "skip_memory": True,
    }

    logger.info("calling agent=%s, role=%s, msg_count=%d, max_tokens=%d",
                agent, role, len(messages), max_tokens)

    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Server response missing choices/content: {e}"
        ) from e

    return content


# ── Response parsers ─────────────────────────────────────────────────────────

_DESIGN_RE = re.compile(
    r"<<<DESIGN>>>\s*(.*?)\s*<<<END>>>", re.DOTALL | re.IGNORECASE,
)
_COMPLEXITY_RE = re.compile(
    r"<<<COMPLEXITY>>>\s*(.*?)\s*<<<END_COMPLEXITY>>>",
    re.DOTALL | re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"<<<FILE:\s*([^\n>]+?)>>>\s*(.*?)\s*<<<END_FILE>>>",
    re.DOTALL | re.IGNORECASE,
)
_REVIEW_RE = re.compile(
    r"<<<REVIEW>>>\s*(.*?)\s*<<<END_REVIEW>>>", re.DOTALL | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"###\s*Verdict\s*\n+\s*(PASS|FAIL)", re.IGNORECASE,
)
# Captures the body of the Verdict Evidence block, terminated by the next
# `### Heading` or end-of-text. Anchors the LLM's verdict to specific evidence
# (acceptance criteria → test for PASS, file:line for FAIL); the parser
# downgrades a missing/empty body to FAIL regardless of stated verdict.
_VERDICT_EVIDENCE_RE = re.compile(
    r"###\s*Verdict\s+Evidence\s*\n+(.*?)(?=\n###\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class ArchitectResult:
    design_md: str
    raw: str
    # Optional — parsed from the COMPLEXITY block. Older architect outputs and
    # malformed blocks leave this None; orchestrator falls back to the env
    # default implementer in that case.
    complexity: Optional[dict] = None


@dataclass
class ImplementerResult:
    files: list[tuple[str, str]]  # (relative_path, content)
    raw: str


@dataclass
class ReviewerResult:
    test_files: list[tuple[str, str]]  # (relative_path, content)
    review_md: str
    verdict: str  # "PASS" or "FAIL"
    raw: str


@dataclass
class ParseError:
    reason: str
    raw: str


def parse_architect_response(text: str) -> ArchitectResult | ParseError:
    cleaned = _strip_thinking(text)
    m = _DESIGN_RE.search(cleaned)
    if not m:
        return ParseError("No <<<DESIGN>>>…<<<END>>> block found", text)
    design = m.group(1).strip()
    if not design:
        return ParseError("<<<DESIGN>>> block was empty", text)
    complexity = _parse_complexity_block(cleaned)
    return ArchitectResult(design_md=design, raw=text, complexity=complexity)


def _parse_complexity_block(cleaned_text: str) -> Optional[dict]:
    """Extract and validate the <<<COMPLEXITY>>> block.

    Returns None on missing/malformed input — the orchestrator handles the
    None case by falling back to the env-default implementer. We deliberately
    do NOT raise ParseError for a missing complexity block: backwards-compat
    matters more than enforcing a brand-new field on the architect, and the
    fallback is safe.
    """
    m = _COMPLEXITY_RE.search(cleaned_text)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None

    fields = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()

    tier = (fields.get("tier") or "").lower()
    if tier not in TIER_TO_IMPLEMENTER:
        # Unknown tier — return what we got but flag for telemetry
        tier = ""
    rec = (fields.get("recommended agent") or fields.get("recommended_agent") or "").strip()
    if rec and rec not in ALLOWED_IMPLEMENTER_AGENTS:
        rec = ""  # silently drop — _select_implementer_agent will fall back
    justification = (fields.get("justification") or "").strip()

    return {
        "tier": tier or None,
        "recommended_agent": rec or None,
        "justification": justification or None,
    }


_KNOWN_CODE_LANG_TAGS = frozenset({
    "python", "py",
    "ts", "typescript", "tsx",
    "js", "javascript", "jsx", "mjs", "cjs",
    "json", "yaml", "yml", "toml", "ini", "xml",
    "html", "css", "scss", "sass",
    "rust", "rs", "go", "java", "kotlin", "kt",
    "swift", "c", "cpp", "cc", "h", "hpp", "objc", "m",
    "ruby", "rb", "php", "perl", "pl",
    "sh", "bash", "zsh", "fish",
    "sql", "graphql", "proto",
    "dockerfile", "makefile", "cmake",
    "lua", "r", "scala", "haskell", "hs", "elixir", "ex", "erlang", "erl",
})


def _strip_markdown_fence(content: str) -> str:
    """Remove a markdown ``` code fence wrapping the file body.

    The implementer is told to write raw file content, but it sometimes
    formats responses as a fenced code block — leaving the fence in
    causes SyntaxError on import.

    Three cases handled:
      1. Fully wrapped: open fence on first line, close fence on last
         line. Both stripped.
      2. Leading fence only (model forgot to close, or output truncated
         by max_tokens). Stripped only when the language tag is a known
         code lang — a bare ``` with no tag is left alone because a
         markdown doc could legitimately open with one.
      3. No fences anywhere: content returned unchanged.

    Trailing-fence-only (close without open) is left alone — that
    pattern shows up in legitimate `.md` files and shouldn't be
    truncated.
    """
    s = content.strip()
    if not s.startswith("```"):
        return content

    first_nl = s.find("\n")
    if first_nl == -1:
        return content  # single line starting with ``` — too ambiguous

    fully_wrapped = s.endswith("```") and first_nl < len(s) - 3
    if fully_wrapped:
        body = s[first_nl + 1 : -3].rstrip()
        return body + "\n" if not body.endswith("\n") else body

    # Leading fence only. Strip when the language tag clearly identifies
    # this as a code wrap, not legitimate markdown content.
    opener = s[:first_nl].strip()       # e.g. "```python"
    lang = opener[3:].strip().lower()   # everything after the backticks
    if lang in _KNOWN_CODE_LANG_TAGS:
        body = s[first_nl + 1 :].rstrip()
        return body + "\n" if not body.endswith("\n") else body

    # Bare ``` with no language, or unrecognized tag — preserve as-is.
    return content


def parse_implementer_response(text: str) -> ImplementerResult | ParseError:
    cleaned = _strip_thinking(text)
    matches = _FILE_RE.findall(cleaned)
    if not matches:
        return ParseError("No <<<FILE: path>>>…<<<END_FILE>>> blocks found", text)
    files = [(path.strip(), _strip_markdown_fence(content)) for path, content in matches]
    return ImplementerResult(files=files, raw=text)


def parse_reviewer_response(text: str) -> ReviewerResult | ParseError:
    cleaned = _strip_thinking(text)

    # Extract test files (same FILE marker as implementer)
    test_files = [(p.strip(), c) for p, c in _FILE_RE.findall(cleaned)]

    # Extract review report
    review_match = _REVIEW_RE.search(cleaned)
    if not review_match:
        return ParseError("No <<<REVIEW>>>…<<<END_REVIEW>>> block found", text)
    review_md = review_match.group(1).strip()

    # Extract verdict
    verdict_match = _VERDICT_RE.search(review_md)
    verdict = verdict_match.group(1).upper() if verdict_match else "FAIL"

    # Layer 3 (anti-hallucination guard): the reviewer must back its verdict
    # with structured evidence — acceptance-criterion → test mapping for PASS,
    # or file:line defects for FAIL. Empty/missing evidence downgrades the
    # verdict to FAIL with a diagnostic appended to the review_md so the
    # implementer retry sees why.
    evidence_match = _VERDICT_EVIDENCE_RE.search(review_md)
    evidence_body = evidence_match.group(1).strip() if evidence_match else ""
    if verdict == "PASS" and not evidence_body:
        verdict = "FAIL"
        review_md += (
            "\n\n---\n\n"
            "**[orchestrator guard]** Verdict downgraded to FAIL: the reviewer "
            "stated PASS but did not provide the required `### Verdict Evidence` "
            "block (acceptance-criterion → test-function mapping). An unanchored "
            "verdict is treated as a hallucination and rejected."
        )

    return ReviewerResult(
        test_files=test_files,
        review_md=review_md,
        verdict=verdict,
        raw=text,
    )


# ── User message builders ───────────────────────────────────────────────────

def build_architect_message(spec_md: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": ARCHITECT_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "## Specification\n\n"
            f"{spec_md}\n\n---\n\n"
            "Your task: produce a complete architecture design for this project. "
            "Output exactly one <<<DESIGN>>>…<<<END>>> block as instructed."
        )},
    ]


def build_implementer_message(
    spec_md: str,
    design_md: str,
    rejection_notes: str | None = None,
) -> list[dict[str, str]]:
    user_parts = [
        "## Specification\n\n",
        spec_md,
        "\n\n## Architecture Design\n\n",
        design_md,
    ]
    if rejection_notes:
        user_parts.extend([
            "\n\n## Previous Attempt — Review Feedback\n\n",
            rejection_notes,
            "\n\n---\n\n"
            "Your task: fix the issues identified above and re-implement. "
            "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for EVERY file. "
            "You must output ALL files again (complete files, not diffs).",
        ])
    else:
        user_parts.extend([
            "\n\n---\n\n"
            "Your task: implement ALL components described in the design. "
            "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for every file. "
            "Paths are relative to the project workspace.",
        ])
    return [
        {"role": "system", "content": IMPLEMENTER_SYSTEM_PROMPT},
        {"role": "user", "content": "".join(user_parts)},
    ]


def build_reviewer_message(
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    test_framework: str = "pytest",
    rejection_notes: Optional[str] = None,
) -> list[dict[str, str]]:
    file_sections = []
    for path, content in code_files:
        file_sections.append(f"### {path}\n```\n{content}\n```\n")

    retry_block = ""
    if rejection_notes:
        retry_block = (
            "\n\n## Prior reviewer attempt failed — fix this before resubmitting\n\n"
            f"{rejection_notes}\n\n"
            "Apply this feedback in your next test file. Do not repeat the same "
            "defect. If the feedback names a missing import, add it; if it names "
            "a broken assertion, fix it.\n"
        )

    return [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "## Specification\n\n"
            f"{spec_md}\n\n"
            "## Architecture Design\n\n"
            f"{design_md}\n\n"
            "## Implementation Files\n\n"
            + "\n".join(file_sections)
            + retry_block
            + f"\n\n---\n\n"
            f"Test framework: {test_framework}\n\n"
            "Your task: write test files and review the implementation. "
            "Output <<<FILE: test_*.py>>>…<<<END_FILE>>> blocks for test files, "
            "then a <<<REVIEW>>>…<<<END_REVIEW>>> block with your verdict."
        )},
    ]


# ── Test runner ──────────────────────────────────────────────────────────────

def _sandbox_available() -> bool:
    """True if we can sandbox test execution with bubblewrap on this host."""
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def _wrap_in_sandbox(cmd: list[str], spec_dir: Path) -> list[str]:
    """Wrap `cmd` in a bubblewrap sandbox.

    The sandbox denies the LLM-generated tests access to anything outside the
    spec workspace:

      - `--unshare-all` creates fresh user/ipc/pid/uts/cgroup/net namespaces,
        so the tests cannot see host processes and have no network (not even
        loopback).
      - `/home` and `/root` are masked with tmpfs so secrets (`.env`, `.ssh`,
        API tokens, browser profiles, etc.) are invisible.
      - `/usr`, `/etc`, `/bin`, `/lib*`, `/opt` are bound read-only so Python
        and pytest can still import system libraries.
      - The venv holding the running Python + pytest is bound read-only.
      - `spec_dir` is bound read-write so pytest can create `.pytest_cache`
        and tests can write their own fixtures.
      - `--clearenv` strips inherited env vars — tests see a minimal,
        predictable environment.

    Tests that legitimately need network or host access won't work under this
    sandbox; set QWEN_ALLOW_UNSANDBOXED_TESTS=1 to opt out at your own risk.
    """
    # Walk up from sys.executable WITHOUT resolving symlinks — venv pythons
    # are typically a symlink chain (`venv/bin/python -> python3 -> /usr/bin/python3`)
    # and .resolve() follows it all the way to /usr, so `--ro-bind /usr /usr`
    # would replace the venv bind and `sys.executable`'s own path would be
    # invisible inside the sandbox.
    venv_root = Path(sys.executable).absolute().parent.parent
    spec_abs = spec_dir.resolve()
    return [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONUNBUFFERED", "1",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        # Baseline filesystem — read-only
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/lib32", "/lib32",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/etc", "/etc",
        "--ro-bind-try", "/opt", "/opt",
        # Fresh kernel interfaces and writable tmp
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
        # Hide every user home, then re-expose just what we need
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--ro-bind", str(venv_root), str(venv_root),
        "--bind", str(spec_abs), str(spec_abs),
        "--chdir", str(spec_abs),
        "--",
    ] + cmd


# Per-framework default timeouts (seconds). Swift/Xcode builds are slow,
# especially cold, so their defaults are generous.
DEFAULT_TIMEOUTS: dict[str, int] = {
    "pytest": 120,
    "python": 120,
    "jest": 120,
    "swift_test": 300,
    "xcodebuild_test": 900,
}

MAC_RUNNER_URL = os.getenv("MAC_RUNNER_URL", "http://127.0.0.1:5050")
MAC_RUNNER_API_KEY = os.getenv("MAC_RUNNER_API_KEY", "")

# Relative paths inside spec_dir that should never be shipped to the Mac
# runner as patch content (they're not part of the LLM's diff).
_SPEC_SKIP_PATTERNS = (".pytest_cache", "__pycache__", ".DS_Store", "test_output.txt")


def _run_local_tests(spec_dir: Path, framework: str, timeout: int) -> tuple[bool, str]:
    """Run pytest/jest locally (bwrap sandbox on Linux).

    LLM-generated test code runs inside a bubblewrap sandbox by default. If
    bwrap is unavailable, the test run fails with a clear diagnostic unless
    QWEN_ALLOW_UNSANDBOXED_TESTS is explicitly set.
    """
    if framework == "jest":
        raw_cmd = ["npx", "jest", "--no-coverage", "--roots", str(spec_dir)]
    else:
        raw_cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short", str(spec_dir)]

    allow_unsandboxed = os.getenv("QWEN_ALLOW_UNSANDBOXED_TESTS", "").lower() in ("1", "true", "yes")

    # The env var takes priority over bwrap detection: if the user explicitly
    # opted out, honor it — even when bwrap is installed but broken (e.g.
    # AppArmor restricting unprivileged user namespaces, which silently
    # makes every bwrap invocation fail with "Operation not permitted"
    # before pytest gets a chance to run).
    if allow_unsandboxed:
        cmd = raw_cmd
        sandbox_mode = "UNSANDBOXED (QWEN_ALLOW_UNSANDBOXED_TESTS=1)"
        logger.warning(
            "running LLM-generated tests WITHOUT a sandbox — tests have full "
            "access to this user's environment"
        )
    elif _sandbox_available():
        cmd = _wrap_in_sandbox(raw_cmd, spec_dir)
        sandbox_mode = "bwrap"
    else:
        msg = (
            "Refusing to run LLM-generated tests: bwrap (bubblewrap) is not "
            "available and QWEN_ALLOW_UNSANDBOXED_TESTS is not set. Install "
            "bubblewrap (e.g. `apt install bubblewrap` on Debian/Ubuntu) on "
            "the Linux server, or set QWEN_ALLOW_UNSANDBOXED_TESTS=1 to opt "
            "out (not recommended — tests run with the orchestrator's own "
            "privileges)."
        )
        logger.error(msg)
        return False, msg

    logger.info("running tests via %s: %s (timeout=%ds)",
                sandbox_mode, " ".join(raw_cmd), timeout)

    try:
        result = subprocess.run(
            cmd, cwd=spec_dir, capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0
    except subprocess.TimeoutExpired:
        output = f"Tests timed out after {timeout}s"
        passed = False
    except Exception as e:
        output = f"Test runner failed: {type(e).__name__}: {e}"
        passed = False

    return passed, output.strip()


def _collect_patch_files(spec_dir: Path) -> tuple[list[dict], Optional[str]]:
    """Enumerate spec_dir as UTF-8 patch files for the Mac runner.

    Returns (patch_files, error). On binary-content encounter, returns
    ([], error_message) so the caller can fail fast.
    """
    patch_files: list[dict] = []
    spec_root = spec_dir.resolve()
    for p in sorted(spec_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(spec_root).as_posix()
        if any(skip in rel.split("/") for skip in _SPEC_SKIP_PATTERNS):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return [], f"non-UTF8 file in spec: {rel} (binary patches not supported)"
        patch_files.append({"path": rel, "content": content})
    return patch_files, None


def _run_mac_runner_tests(
    spec_dir: Path,
    framework: str,
    timeout: int,
    *,
    repo: Optional[str],
    base_ref: str = "HEAD",
    scheme: Optional[str] = None,
    destination: Optional[str] = None,
    configuration: Optional[str] = None,
    workspace: Optional[str] = None,
    project: Optional[str] = None,
    filter: Optional[str] = None,
) -> tuple[bool, str]:
    """Dispatch swift_test / xcodebuild_test to the Mac runner over HTTP."""
    if not MAC_RUNNER_API_KEY:
        return False, (
            "MAC_RUNNER_API_KEY is not set on the orchestrator. Configure "
            "MAC_RUNNER_URL and MAC_RUNNER_API_KEY in ~/.config/qwen-server/.env "
            "to dispatch Swift/Xcode tests to the Mac runner."
        )
    if not repo:
        return False, (
            f"{framework} requires a 'repo' (symbolic name registered in the "
            f"Mac runner's repos.yml). Add it to the spec's test_strategy block."
        )

    patch_files, err = _collect_patch_files(spec_dir)
    if err:
        return False, err

    payload: dict = {
        "spec_id": spec_dir.name,
        "repo": repo,
        "base_ref": base_ref,
        "patch_files": patch_files,
        "framework": framework,
        "timeout": timeout,
    }
    for key, val in (("scheme", scheme), ("destination", destination),
                     ("configuration", configuration), ("workspace", workspace),
                     ("project", project), ("filter", filter)):
        if val is not None:
            payload[key] = val

    url = f"{MAC_RUNNER_URL.rstrip('/')}/v1/run_tests"
    headers = {"X-Runner-Key": MAC_RUNNER_API_KEY}
    # Give the HTTP call headroom beyond the test timeout so the runner can
    # finish packaging the response even on a long run.
    http_timeout = timeout + 30

    logger.info("dispatching %s to mac-runner %s (timeout=%ds, %d files)",
                framework, url, timeout, len(patch_files))
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=http_timeout)
    except requests.RequestException as e:
        return False, f"mac-runner unreachable at {url}: {e}"

    if resp.status_code != 200:
        return False, f"mac-runner HTTP {resp.status_code}: {resp.text[:2000]}"

    try:
        data = resp.json()
    except ValueError:
        return False, f"mac-runner returned non-JSON response: {resp.text[:2000]}"

    return bool(data.get("passed")), str(data.get("output", ""))


def run_tests(
    spec_dir: Path,
    framework: str = "pytest",
    timeout: Optional[int] = None,
    **framework_opts,
) -> tuple[bool, str]:
    """Run tests for a spec.

    Dispatches by framework:
      - pytest / python / jest   → local (bwrap sandbox on Linux)
      - swift_test               → Mac runner HTTP dispatch; requires `repo`
      - xcodebuild_test          → Mac runner HTTP dispatch; requires `repo` + `scheme`

    framework_opts carries the framework-specific configuration from the
    planner's test_strategy block (repo, base_ref, scheme, destination,
    configuration, workspace, project, filter) — unknown keys are ignored.

    Returns (passed, combined_output).
    """
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUTS.get(framework, 120)

    if framework in ("swift_test", "xcodebuild_test"):
        passed, output = _run_mac_runner_tests(
            spec_dir, framework, effective_timeout,
            repo=framework_opts.get("repo"),
            base_ref=framework_opts.get("base_ref", "HEAD"),
            scheme=framework_opts.get("scheme"),
            destination=framework_opts.get("destination"),
            configuration=framework_opts.get("configuration"),
            workspace=framework_opts.get("workspace"),
            project=framework_opts.get("project"),
            filter=framework_opts.get("filter"),
        )
    else:
        passed, output = _run_local_tests(spec_dir, framework, effective_timeout)

    logger.info("test result: %s (%d chars output)",
                "PASS" if passed else "FAIL", len(output))
    return passed, (output or "").strip()
