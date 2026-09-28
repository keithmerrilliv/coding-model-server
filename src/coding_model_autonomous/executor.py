"""The agent layer's former single module, kept as a re-exporting façade.

The agent layer lives in six modules:

* ``settings``  — every env knob (agents, budgets, parse retries, modes)
* ``prompts``   — the roles' system prompts
* ``_http``     — the HTTP transport and ``call_agent``
* ``parsers``   — parsers for the agents' <<<MARKER>>> responses
* ``messages``  — what each role is sent, and the design-sized budgets
* ``normalize`` — deterministic fixes and scans on generated files

Every name below lives in the module it is imported from and is re-exported
here only so existing importers keep working. Read and patch a knob on
``settings``, and a function in its home module: a patch on this façade
rebinds only this module's copy, which nothing reads.

Every agent call is synchronous and blocks the daemon's tick thread for the
whole inference. That is intentional: one GPU and a sequential inference
lock leave the daemon nothing else to do in parallel.
"""

from __future__ import annotations

from .settings import (  # noqa: F401
    ARCHITECT_AGENT,
    IMPLEMENTER_AGENT,
    REVIEWER_AGENT,
    DESIGN_REVIEW_ENABLED,
    DESIGN_REVIEW_AGENT,
    DESIGN_REVIEW_MAX_TOKENS,
    DESIGN_REVIEW_MAX_REVISIONS,
    TESTABILITY_CHECK_ENABLED,
    TESTABILITY_CHECK_MAX_ROUNDS,
    REVIEWER_PARSE_RETRIES,
    _parse_memory_roles,
    AUTONOMOUS_MEMORY_ROLES,
    _LANGUAGE_ALIASES,
    normalize_language,
    _EXTENSION_LANGUAGES,
    language_from_paths,
    AUTONOMOUS_MEMORY_LANGUAGES,
    retrieval_decision,
    ARCHITECT_TIMEOUT,
    IMPLEMENTER_TIMEOUT,
    REVIEWER_TIMEOUT,
    ARCHITECT_MAX_TOKENS,
    ARCHITECT_RETRY_MAX_TOKENS,
    architect_max_tokens,
    IMPLEMENTER_MAX_TOKENS,
    REVIEWER_MAX_TOKENS,
    IMPLEMENTER_MAX_TOKENS_CEILING,
    IMPLEMENTER_TOKENS_PER_FILE,
    IMPLEMENTER_TOKENS_BASE,
    MAX_RETRIES,
    DIFF_BASED_EDITS,
    IMPLEMENTER_MODE,
    MANIFEST_FILE_THRESHOLD,
    MANIFEST_MAX_TOKENS,
    PER_FILE_MAX_TOKENS,
    PER_FILE_PARSE_RETRIES,
    MANIFEST_WHOLE_FILE_MAX_CHARS,
    SYNTHESIS_EMIT_HEADROOM,
    ARCHITECT_PARSE_RETRIES,
    MANIFEST_PARSE_RETRIES,
    ROLE_TO_AGENT,
    ROLE_TO_TIMEOUT,
    ROLE_TO_MAX_TOKENS,
    role_to_agent,
    EXISTING_FILES_MAX_CHARS,
    PROTECTED_FILES_MAX_CHARS,
    PRIOR_ARTIFACTS_MAX_CHARS,
)
from .prompts import (  # noqa: F401
    ARCHITECT_SYSTEM_PROMPT,
    SWIFT_VALUE_SEMANTICS,
    IMPLEMENTER_SYSTEM_PROMPT,
    IMPLEMENTER_EDIT_MODE_INSTRUCTIONS,
    PER_FILE_EDIT_MODE_INSTRUCTIONS,
    REVIEWER_SYSTEM_PROMPT,
    ARCHITECT_TOOL_PROTOCOL,
    DESIGN_REVIEW_SYSTEM_PROMPT,
    MANIFEST_SYSTEM_PROMPT,
    PER_FILE_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
)
from .parsers import (  # noqa: F401
    _strip_thinking,
    _DESIGN_RE,
    _DESIGN_FUZZY_RE,
    _ANY_DELIMITER_RE,
    _COMPLEXITY_RE,
    _FILE_RE,
    _REVIEW_RE,
    _VERDICT_RE,
    _CITATION_RE,
    _TEST_DECL_RES,
    _unresolvable_citations,
    _VERDICT_EVIDENCE_RE,
    _missing_verdict_reason,
    ArchitectResult,
    ImplementerResult,
    ReviewerResult,
    ParseError,
    parse_architect_response,
    _parse_complexity_block,
    _KNOWN_CODE_LANG_TAGS,
    _strip_markdown_fence,
    _dedupe_files_last_wins,
    _TEMPLATE_PLACEHOLDER_LINE_RE,
    _strip_template_placeholder,
    parse_implementer_response,
    parse_reviewer_response,
    parse_design_review,
    ManifestEntry,
    ManifestResult,
    _MANIFEST_RE,
    _MANIFEST_FUZZY_RE,
    _LIST_MARKER_RE,
    parse_manifest_response,
)
from .normalize import (  # noqa: F401
    pin_version,
    _pin_package_json,
    _FOUNDATION_ONLY_SYMBOLS,
    _FOUNDATION_REEXPORTERS,
    _SWIFT_IMPORT_RE,
    _SWIFT_DECL_RE,
    _swift_code_only,
    _first_swift_code_line,
    declared_top_level_types,
    protected_type_collisions,
    _ensure_foundation_import,
    normalize_boilerplate,
    _blank_ts_comments_and_strings,
    _ANY_TYPE_RE,
    find_any_type_violations,
    scan_any_violations,
    _TAUTOLOGY_PATTERNS,
    _TEST_FILE_PATH_RE,
    scan_tautological_asserts,
)
from .messages import (  # noqa: F401
    whole_file_emission_tokens,
    _DESIGN_FILE_PATH_RE,
    _file_structure_section,
    estimate_design_file_count,
    implementer_max_tokens_for,
    _render_plan_constraints,
    _MEMORY_QUERY_MAX_CHARS,
    spec_memory_query,
    file_memory_query,
    _render_reference_files,
    _render_approval_conditions,
    _render_unreadable_modifications,
    build_architect_message,
    build_design_review_message,
    _SRC_PACKAGE_RE,
    import_packages,
    render_import_root,
    _render_file_modes,
    _render_existing_files,
    build_implementer_message,
    _DECL_MODIFIER,
    _DECL_KEYWORD,
    _DECL_PROPERTY,
    _DECL_ENUM_CASE,
    _SIGNATURE_RE,
    _DESIGN_UNIT_PATH_RE,
    _BARE_FILENAME_RE,
    _TREE_NODE_RE,
    _NUMBER_WORDS,
    _PROSE_COUNT_RE,
    _prose_unit_count,
    estimate_design_unit_count,
    use_manifest_mode,
    summarize_written_files,
    build_manifest_message,
    build_per_file_message,
    build_reviewer_message,
    build_synthesis_message,
    build_synthesis_repair_message,
)
from ._http import (  # noqa: F401
    call_agent,
    agent_event_fields,
    accumulate_agent_fields,
)
from ._http import post_chat_completion  # noqa: F401
from .retry_policy import ALLOWED_IMPLEMENTER_AGENTS, TIER_TO_IMPLEMENTER  # noqa: F401
from .workspace import _count_declarations, artifact_path  # noqa: F401
