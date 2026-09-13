"""DEV-655: instructional text must never parse as a file write.

Runs 28 and 29 both landed a file literally named `{p}` whose whole content
was `...` — the File-modes prompt section rendered a complete
`<<<FILE: path>>> ... <<<END_FILE>>>` pair around a real path, a model echoed
the instruction, and the parser took the echo for a write. Reproduced with no
model: the prompt section alone parsed as a file. Two layers now: the prompt
never closes a marker pair around a real path on one line, and the parser
drops — and names — a block whose body is only an ellipsis.
"""
from coding_model_autonomous.executor import (
    ImplementerResult, _render_file_modes, parse_implementer_response,
)
from coding_model_autonomous.apply_edits import apply_search_replace  # noqa: F401  (import guard)

EXISTING = ["src/coding_model_autonomous/executor.py"]
NEW = ["tests/test_import_root_guidance.py"]


def test_the_file_modes_section_does_not_parse_as_a_write():
    """The ticket's own reproduction. Failed before the fix."""
    section = _render_file_modes(EXISTING, NEW)
    r = parse_implementer_response(section)
    assert getattr(r, "files", []) == []


def test_the_section_still_names_the_opener_and_the_mode():
    section = _render_file_modes(EXISTING, NEW)
    assert "EMIT WHOLE — new file: `tests/test_import_root_guidance.py`" in section
    assert "<<<FILE: tests/test_import_root_guidance.py>>>" in section
    assert "EDIT ONLY — existing: `src/coding_model_autonomous/executor.py`" in section
    # the closer is described, never rendered next to the opener
    assert "<<<END_FILE>>>" not in section


def test_an_ellipsis_only_block_is_dropped_and_named():
    """Defence in depth: an echoed template with only `...` for a body."""
    text = ("<<<FILE: {p}>>>\n...\n<<<END_FILE>>>\n\n"
            "<<<FILE: tests/test_real.py>>>\ndef test_a(): pass\n<<<END_FILE>>>\n")
    r = parse_implementer_response(text)
    assert isinstance(r, ImplementerResult)
    assert [p for p, _ in r.files] == ["tests/test_real.py"]
    assert r.echoed_placeholders == ["{p}"]


def test_a_unicode_ellipsis_counts_too():
    r = parse_implementer_response("<<<FILE: x.py>>>\n…\n<<<END_FILE>>>\n"
                                   "<<<FILE: y.py>>>\nx = 1\n<<<END_FILE>>>\n")
    assert [p for p, _ in r.files] == ["y.py"] and r.echoed_placeholders == ["x.py"]


def test_a_genuinely_empty_body_is_not_treated_as_an_echo_silently():
    """An empty file the model meant is a different fault and stays visible
    to the same field — the daemon names it either way."""
    r = parse_implementer_response("<<<FILE: empty.py>>>\n\n<<<END_FILE>>>\n")
    assert r.files == [] and r.echoed_placeholders == ["empty.py"]


def test_real_content_is_untouched():
    r = parse_implementer_response("<<<FILE: a.py>>>\nprint('...')\n<<<END_FILE>>>\n")
    assert r.files == [("a.py", "print('...')")] and r.echoed_placeholders == []
