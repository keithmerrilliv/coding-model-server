"""Phase b: adversarial test generation via external judges.

Extracted verbatim from executor.py (DEV-152). Self-contained apart from a
few message/parse helpers it shares with the main executor.
"""
from __future__ import annotations

import logging
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coding_model_server import external_judges

from .executor import (
    _FILE_RE,
    _strip_markdown_fence,
    _strip_thinking,
    _write_artifact,
)

logger = logging.getLogger("orchestrator.adversarial")


# ── Phase b: Adversarial test generation (Gemini and/or Claude) ──────────────
#
# Fires once per spec, after the local Coding Model reviewer's tests pass on retry-0,
# before creating the release_approval gate. Each configured provider is given
# the spec, design, code, and Coding Model's existing tests, and asked to write
# additional pytest tests targeting edge cases Coding Model missed. Files land in
# spec_dir under the `adversarial_test_*.py` namespace.
#
# Provider modes (AUTONOMOUS_ADVERSARIAL_PROVIDER):
#   - gemini (default): single-provider, files named `adversarial_test_*.py`.
#   - claude:           single-provider, same namespace.
#   - both:             both providers fire sequentially. Each gets a
#                       provider-tagged filename prefix
#                       (`adversarial_test_claude_*.py` /
#                       `adversarial_test_gemini_*.py`) so they can't
#                       overwrite each other's files.
#
# Disabled by default. Set AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED=1 to opt in.
# Per-provider failure (key/SDK/network/timeout) is caught inside the loop —
# the next provider still runs and the orchestrator's outer try/except keeps
# the original PASS standing if everything fails.

ADVERSARIAL_TESTS_ENABLED = os.getenv(
    "AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED", "0"
).lower() in ("1", "true", "yes")
ADVERSARIAL_MAX_TOKENS = int(os.getenv("AUTONOMOUS_ADVERSARIAL_MAX_TOKENS", "8000"))
ADVERSARIAL_TIMEOUT = float(os.getenv("AUTONOMOUS_ADVERSARIAL_TIMEOUT", "300"))

ADVERSARIAL_PROVIDER = os.getenv("AUTONOMOUS_ADVERSARIAL_PROVIDER", "gemini").lower()
ADVERSARIAL_GEMINI_MODEL = os.getenv(
    "AUTONOMOUS_ADVERSARIAL_GEMINI_MODEL", external_judges.DEFAULT_GEMINI_MODEL
)
ADVERSARIAL_CLAUDE_MODEL = os.getenv(
    "AUTONOMOUS_ADVERSARIAL_CLAUDE_MODEL", external_judges.DEFAULT_CLAUDE_MODEL
)

ADVERSARIAL_FILENAME_PREFIX = "adversarial_test_"


@dataclass
class AdversarialProviderResult:
    """Per-provider outcome from one Phase b firing.

    The orchestrator records one AGENT_RAN event per result so the stats
    script can attribute false-FAILs to a specific provider.
    """
    provider: str                          # "claude" | "gemini"
    model: str                             # the actual model name used
    files_written: list[tuple[str, str]]   # (basename, content)
    error: Optional[str] = None            # "<ExcType>: <message>" if call raised
    skipped: bool = False                  # True when provider returned 0 blocks


def _resolve_providers() -> list[str]:
    """Map the env var to an ordered provider list.

    Order matters only for telemetry/log readability. Tests re-run reads
    everything from disk regardless of order.
    """
    p = ADVERSARIAL_PROVIDER
    if p == "gemini":
        return ["gemini"]
    if p == "claude":
        return ["claude"]
    if p == "both":
        return ["claude", "gemini"]
    logger.warning(
        "AUTONOMOUS_ADVERSARIAL_PROVIDER=%r is invalid (expected "
        "gemini|claude|both); falling back to gemini",
        p,
    )
    return ["gemini"]


def _provider_model(provider: str) -> str:
    if provider == "claude":
        return ADVERSARIAL_CLAUDE_MODEL
    if provider == "gemini":
        return ADVERSARIAL_GEMINI_MODEL
    raise ValueError(f"unknown provider: {provider}")


def _provider_filename_prefix(provider: str, multi_provider: bool) -> str:
    """Return the filename prefix this provider must use.

    Single-provider mode keeps the original `adversarial_test_` namespace
    (backwards-compatible with the v1 design). 'both' mode uses
    `adversarial_test_<provider>_` so concurrent files can't collide.
    """
    if not multi_provider:
        return ADVERSARIAL_FILENAME_PREFIX
    return f"{ADVERSARIAL_FILENAME_PREFIX}{provider}_"


def _build_adversarial_system_prompt(filename_prefix: str) -> str:
    """Build the per-provider system prompt with its filename prefix
    interpolated into rule 2 and the output-format footer."""
    return textwrap.dedent(f"""\
        You are an adversarial test-writer. The local reviewer has already
        written tests and approved this code. Your job: write 3 to 7 additional
        pytest tests that target edge cases the reviewer's tests miss.

        # What to target
        - Boundary values: empty inputs, single-element inputs, max-size inputs.
        - Type edge cases: None, NaN, negative numbers where positive expected.
        - Concurrent / ordering edge cases when the spec implies order.
        - Error paths: malformed inputs, missing files, timeout/retry.
        - Off-by-one in indices, slices, ranges.

        # Hard rules

        1. Test ONLY behaviors the spec requires. If the spec is silent on an
           edge case, do NOT invent a requirement. Over-specification is the
           failure mode you must avoid.

           Concrete example of what NOT to test: if the spec asks for a
           function that returns the sum of a list of ints and is silent on
           what to do with floats, do NOT add a test that asserts a specific
           behavior for floats. Skip it.

        2. Output one or more `<<<FILE: {filename_prefix}<name>.py>>>` blocks
           terminated by `<<<END_FILE>>>`. Filenames MUST start with
           `{filename_prefix}` so they don't collide with the reviewer's
           tests or with another adversarial provider's tests. Any file
           whose name doesn't start with `{filename_prefix}` will be
           silently dropped by the orchestrator.

        3. Tests must pass on a CORRECT implementation. If you cannot tell
           whether the spec requires the behavior you want to test, skip it.

        4. Use pytest. Real imports, no mocks beyond what the existing tests
           already use.

        5. At the top of each test function, add a one-line docstring naming
           the spec acceptance criterion the test exercises:
               def test_negative_count():
                   \"\"\"Acceptance: 'rejects negative counts with ValueError'.\"\"\"

        6. If, after careful reading, you conclude the reviewer's tests
           already cover every edge case the spec requires, output NO file
           blocks at all. An empty response is the correct answer in that
           case — do not invent tests for the sake of producing output.

        # Output format

        Either zero file blocks (if no useful adversarial tests exist), or
        1 to 3 files containing 3-7 test functions total. No prose outside
        the file blocks.
        """)


def _call_provider(provider: str, system_prompt: str, user_content: str) -> str:
    if provider == "claude":
        return external_judges.call_claude(
            system_prompt, user_content,
            max_tokens=ADVERSARIAL_MAX_TOKENS,
            timeout=ADVERSARIAL_TIMEOUT,
            model=ADVERSARIAL_CLAUDE_MODEL,
        )
    if provider == "gemini":
        return external_judges.call_gemini(
            system_prompt, user_content,
            max_tokens=ADVERSARIAL_MAX_TOKENS,
            timeout=ADVERSARIAL_TIMEOUT,
            model=ADVERSARIAL_GEMINI_MODEL,
        )
    raise ValueError(f"unknown provider: {provider}")


def adversarial_tests_available() -> tuple[bool, Optional[str]]:
    """Pre-flight: (ok, reason_if_not). Used by orchestrator startup logging.

    Returns the FIRST configured-provider failure encountered. ok=True
    means every configured provider has both its key and SDK present.
    """
    if not ADVERSARIAL_TESTS_ENABLED:
        return False, "AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED is not set"
    for provider in _resolve_providers():
        if provider == "claude":
            ok, reason = external_judges.claude_available()
        elif provider == "gemini":
            ok, reason = external_judges.gemini_available()
        else:
            return False, f"unknown provider: {provider}"
        if not ok:
            return False, f"provider={provider}: {reason}"
    return True, None


def build_adversarial_test_message(
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    reviewer_tests: list[tuple[str, str]],
    reviewer_test_output: str,
) -> str:
    """Assemble the user-content payload for the adversarial test-writer.

    Mirrors the structure of build_reviewer_message but adds the
    reviewer's tests and the pytest output proving they passed — that's
    the adversarial framing ("here's what already passes; break it").
    The same user-content is sent to every configured provider.
    """
    code_sections = [f"### {p}\n```\n{c}\n```\n" for p, c in code_files]
    reviewer_test_sections = [
        f"### {p}\n```\n{c}\n```\n" for p, c in reviewer_tests
    ]
    truncated_test_output = reviewer_test_output[-4000:] if reviewer_test_output else ""

    return (
        "## Specification\n\n"
        f"{spec_md}\n\n"
        "## Architecture Design\n\n"
        f"{design_md}\n\n"
        "## Implementation Files (the code under test)\n\n"
        + "\n".join(code_sections)
        + "\n## Reviewer's Existing Tests (already pass — do not duplicate)\n\n"
        + ("\n".join(reviewer_test_sections) if reviewer_test_sections
           else "(no reviewer tests on disk)\n")
        + "\n## Reviewer's Test-Run Output (last 4 KB)\n\n"
        + f"```\n{truncated_test_output}\n```\n\n"
        + "---\n\n"
        "Your task: write 3-7 adversarial pytest tests across 1-3 files. "
        "Each test must exercise a spec-required behavior the reviewer's "
        "tests miss. If you find no missing coverage, output zero file "
        "blocks. Filename rules are in the system prompt — follow them "
        "exactly or your files will be silently dropped."
    )


def _generate_for_provider(
    provider: str,
    expected_prefix: str,
    user_content: str,
    spec_dir: Path,
) -> list[tuple[str, str]]:
    """Call one provider, parse + validate + write its files.

    Returns the list of (basename, content) successfully written. An
    empty list either means the model emitted no blocks (rule 6) or
    every block was rejected for namespace/extension/path-traversal.

    Raises any exception from the SDK call so the outer loop can record
    the failure for telemetry; parse/validation rejects are logged and
    suppressed.
    """
    system_prompt = _build_adversarial_system_prompt(expected_prefix)
    raw = _call_provider(provider, system_prompt, user_content)
    cleaned = _strip_thinking(raw)
    matches = _FILE_RE.findall(cleaned)
    if not matches:
        return []

    written: list[tuple[str, str]] = []
    for rel_path, content in matches:
        rel_path = rel_path.strip()
        # Take just the basename to defang `dir/adversarial_test_x.py`
        # attempts that might escape the namespace check below.
        basename = os.path.basename(rel_path)
        if not basename.startswith(expected_prefix):
            logger.warning(
                "phase-b: rejecting %s adversarial test with non-conforming "
                "filename %r (must start with %r)",
                provider, rel_path, expected_prefix,
            )
            continue
        if not basename.endswith(".py"):
            logger.warning(
                "phase-b: rejecting %s adversarial test with non-.py "
                "extension %r", provider, rel_path,
            )
            continue
        cleaned_content = _strip_markdown_fence(content)
        try:
            _write_artifact(spec_dir, basename, cleaned_content)
        except ValueError as e:
            logger.warning("phase-b: rejecting %s adversarial test %r: %s",
                           provider, rel_path, e)
            continue
        written.append((basename, cleaned_content))
    return written


def generate_adversarial_tests(
    spec_dir: Path,
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    reviewer_tests: list[tuple[str, str]],
    reviewer_test_output: str,
) -> list[AdversarialProviderResult]:
    """Run every configured provider in sequence, return per-provider results.

    Per-provider exceptions are caught and recorded as ``error`` on the
    result; the next provider still runs. The orchestrator records one
    AGENT_RAN event per AdversarialProviderResult and re-runs tests if
    any provider wrote files.
    """
    user_content = build_adversarial_test_message(
        spec_md, design_md, code_files, reviewer_tests, reviewer_test_output,
    )
    providers = _resolve_providers()
    multi = len(providers) > 1
    results: list[AdversarialProviderResult] = []

    for provider in providers:
        prefix = _provider_filename_prefix(provider, multi)
        model = _provider_model(provider)
        logger.info(
            "phase-b: calling %s adversarial test-writer "
            "(model=%s, max_tokens=%d, timeout=%.0fs, prefix=%r, %d KiB user)",
            provider, model, ADVERSARIAL_MAX_TOKENS, ADVERSARIAL_TIMEOUT,
            prefix, len(user_content) // 1024,
        )
        try:
            files = _generate_for_provider(provider, prefix, user_content, spec_dir)
        except Exception as e:  # noqa: BLE001 — fail-open per provider
            err = f"{type(e).__name__}: {e}"
            logger.warning(
                "phase-b: %s adversarial call failed (%s); "
                "remaining providers (if any) will continue",
                provider, err,
            )
            results.append(AdversarialProviderResult(
                provider=provider, model=model, files_written=[], error=err,
            ))
            continue

        if not files:
            logger.info(
                "phase-b: %s returned no adversarial test blocks "
                "(rule 6 — reviewer coverage deemed sufficient, or "
                "every block rejected at validation)",
                provider,
            )
            results.append(AdversarialProviderResult(
                provider=provider, model=model, files_written=[], skipped=True,
            ))
            continue

        results.append(AdversarialProviderResult(
            provider=provider, model=model, files_written=files,
        ))
        logger.info("phase-b: %s wrote %d adversarial test file(s)",
                    provider, len(files))

    total = sum(len(r.files_written) for r in results)
    logger.info("phase-b: %d total adversarial file(s) across %s",
                total, ", ".join(providers))
    return results
