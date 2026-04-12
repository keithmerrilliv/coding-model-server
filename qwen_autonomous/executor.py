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
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("orchestrator.executor")


# ── Configuration ────────────────────────────────────────────────────────────

ARCHITECT_AGENT = os.getenv("AUTONOMOUS_ARCHITECT_AGENT", "q35_architect")
IMPLEMENTER_AGENT = os.getenv("AUTONOMOUS_IMPLEMENTER_AGENT", "implementer")
REVIEWER_AGENT = os.getenv("AUTONOMOUS_REVIEWER_AGENT", "reviewer")

ARCHITECT_TIMEOUT = float(os.getenv("AUTONOMOUS_ARCHITECT_TIMEOUT", "2700"))
IMPLEMENTER_TIMEOUT = float(os.getenv("AUTONOMOUS_IMPLEMENTER_TIMEOUT", "1800"))
REVIEWER_TIMEOUT = float(os.getenv("AUTONOMOUS_REVIEWER_TIMEOUT", "1200"))

ARCHITECT_MAX_TOKENS = int(os.getenv("AUTONOMOUS_ARCHITECT_MAX_TOKENS", "8000"))
IMPLEMENTER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_IMPLEMENTER_MAX_TOKENS", "16000"))
REVIEWER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_REVIEWER_MAX_TOKENS", "12000"))

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
    """
    # Strip leading slashes only — do NOT use lstrip("./") which eats
    # individual chars and would normalize "../../../x" into "x".
    while rel_path.startswith("/"):
        rel_path = rel_path[1:]
    abs_path = (spec_dir / rel_path).resolve()
    if not str(abs_path).startswith(str(spec_dir.resolve())):
        raise ValueError(f"Path traversal rejected: {rel_path}")
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content)
    return abs_path


def role_to_agent(role: str) -> str:
    return ROLE_TO_AGENT.get(role, IMPLEMENTER_AGENT)


# ── System prompts ───────────────────────────────────────────────────────────

ARCHITECT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the ARCHITECT agent for an autonomous software development service.
    Your job: read the specification and produce a design document that the
    IMPLEMENTER can execute exactly, without ambiguity.

    # Output format

    Respond with EXACTLY ONE block. No preamble. No text outside the markers.

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

    # Rules

    1. Be prescriptive. The implementer follows your design exactly.
    2. Specify exact file paths relative to the project workspace root.
    3. Keep it concise — the implementer needs clarity, not prose.
    4. Do NOT write implementation code. That is the implementer's job.
    5. Do NOT add scope beyond what the spec requests.
    6. Every acceptance criterion from the spec must appear in your checklist.
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
    7. On a retry attempt (when previous code was rejected), you will see
       the reviewer's feedback. Fix every issue identified and output ALL
       files again — the daemon overwrites previous versions.
    """)

REVIEWER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the REVIEWER agent for an autonomous software development service.
    You receive the specification, the architecture design, and the
    implementation source files. Your job:

    1. Write test files that verify the acceptance criteria.
    2. Review the code for correctness relative to the spec and design.
    3. Report your findings.

    # Output format

    First, output any test files:

    <<<FILE: test_something.py>>>
    <complete test file content>
    <<<END_FILE>>>

    Then output your review report:

    <<<REVIEW>>>
    ## Test Files Written
    - <list each test file and what it covers>

    ## Code Review
    ### Issues Found
    - <severity: critical/major/minor> <file>:<location> <description>

    (If no issues: "No issues found.")

    ### Verdict
    PASS

    (or FAIL if there are critical issues that will cause test failures)

    ### Notes
    <anything the implementer should know if this is sent back for a retry>
    <<<END_REVIEW>>>

    # Rules

    1. Focus on correctness relative to the spec's acceptance criteria.
    2. Write tests that can actually be run — correct imports, real paths,
       no mocked dependencies unless the spec calls for it.
    3. Use the test framework specified in the plan (default: pytest).
    4. If the implementation is complete and correct, verdict is PASS.
    5. Only use FAIL if there are critical issues that will cause failures.
    6. Do NOT add test scope beyond what the spec requires.
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


@dataclass
class ArchitectResult:
    design_md: str
    raw: str


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
    return ArchitectResult(design_md=design, raw=text)


def parse_implementer_response(text: str) -> ImplementerResult | ParseError:
    cleaned = _strip_thinking(text)
    matches = _FILE_RE.findall(cleaned)
    if not matches:
        return ParseError("No <<<FILE: path>>>…<<<END_FILE>>> blocks found", text)
    files = [(path.strip(), content) for path, content in matches]
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
) -> list[dict[str, str]]:
    file_sections = []
    for path, content in code_files:
        file_sections.append(f"### {path}\n```\n{content}\n```\n")

    return [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "## Specification\n\n"
            f"{spec_md}\n\n"
            "## Architecture Design\n\n"
            f"{design_md}\n\n"
            "## Implementation Files\n\n"
            + "\n".join(file_sections)
            + f"\n\n---\n\n"
            f"Test framework: {test_framework}\n\n"
            "Your task: write test files and review the implementation. "
            "Output <<<FILE: test_*.py>>>…<<<END_FILE>>> blocks for test files, "
            "then a <<<REVIEW>>>…<<<END_REVIEW>>> block with your verdict."
        )},
    ]


# ── Test runner ──────────────────────────────────────────────────────────────

def run_tests(
    spec_dir: Path,
    framework: str = "pytest",
    timeout: int = 120,
) -> tuple[bool, str]:
    """Run tests in the spec workspace via subprocess.

    Returns (passed, combined_output). The daemon uses the output to
    build failure reports that get fed back to the implementer on retry.
    """
    if framework in ("pytest", "python"):
        cmd = ["python3", "-m", "pytest", "-v", "--tb=short", str(spec_dir)]
    elif framework == "jest":
        cmd = ["npx", "jest", "--no-coverage", "--roots", str(spec_dir)]
    else:
        # Default to pytest
        cmd = ["python3", "-m", "pytest", "-v", "--tb=short", str(spec_dir)]

    logger.info("running tests: %s (timeout=%ds)", " ".join(cmd), timeout)

    try:
        result = subprocess.run(
            cmd,
            cwd=spec_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0
    except subprocess.TimeoutExpired:
        output = f"Tests timed out after {timeout}s"
        passed = False
    except Exception as e:
        output = f"Test runner failed: {type(e).__name__}: {e}"
        passed = False

    logger.info("test result: %s (%d chars output)",
                "PASS" if passed else "FAIL", len(output))
    return passed, output.strip()
