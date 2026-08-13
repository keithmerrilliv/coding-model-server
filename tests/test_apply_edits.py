"""Pure unit tests for anchored SEARCH/REPLACE application (DEV-581).

Everything here exercises apply_edits with no I/O and no orchestrator: parse
model text into per-file blocks, apply blocks to current content, and combine
new (whole) files with applied edits. The apply must be whitespace-exact, must
require a unique anchor, and must never partially apply.
"""
from coding_model_autonomous import apply_edits
from coding_model_autonomous.apply_edits import (
    EditBlock,
    apply_search_replace,
    parse_edit_blocks,
    resolve_edits,
)


def _blocks(text):
    """Parse and return {path: [EditBlock, ...]} for convenience."""
    parsed = parse_edit_blocks(text)
    return {fe.path: fe.blocks for fe in parsed.files}, parsed.malformed


# ── apply_search_replace ─────────────────────────────────────────────────────

def test_exact_single_block_apply():
    current = "line one\nline two\nline three\n"
    blocks = [EditBlock(search="line two", replace="line 2")]
    out = apply_search_replace(current, blocks)
    assert out.ok
    assert out.content == "line one\nline 2\nline three\n"


def test_multiline_block_preserves_surrounding_content():
    current = "a\nb\nc\nd\n"
    blocks = [EditBlock(search="b\nc", replace="B\nC\nC2")]
    out = apply_search_replace(current, blocks)
    assert out.ok
    assert out.content == "a\nB\nC\nC2\nd\n"


def test_whitespace_sensitive_match_fails_on_different_indentation():
    # SEARCH uses 4 spaces; the file has a tab — must NOT match.
    current = "def f():\n\treturn 1\n"
    blocks = [EditBlock(search="def f():\n    return 1", replace="def f():\n    return 2")]
    out = apply_search_replace(current, blocks)
    assert not out.ok
    assert "not found" in out.error


def test_whitespace_sensitive_match_succeeds_on_exact_indentation():
    current = "def f():\n    return 1\n"
    blocks = [EditBlock(search="    return 1", replace="    return 2")]
    out = apply_search_replace(current, blocks)
    assert out.ok
    assert out.content == "def f():\n    return 2\n"


def test_multiple_blocks_in_one_file_apply_in_order():
    current = "alpha\nbeta\ngamma\n"
    blocks = [
        EditBlock(search="alpha", replace="ALPHA"),
        EditBlock(search="gamma", replace="GAMMA"),
    ]
    out = apply_search_replace(current, blocks)
    assert out.ok
    assert out.content == "ALPHA\nbeta\nGAMMA\n"


def test_search_not_found_is_an_error():
    current = "one\ntwo\n"
    blocks = [EditBlock(search="nonexistent", replace="x")]
    out = apply_search_replace(current, blocks)
    assert not out.ok
    assert "not found" in out.error
    # No content is returned on failure — the caller must not write anything.
    assert out.content is None


def test_search_ambiguous_two_matches_is_an_error():
    current = "dup\nmiddle\ndup\n"
    blocks = [EditBlock(search="dup", replace="X")]
    out = apply_search_replace(current, blocks)
    assert not out.ok
    assert "ambiguous" in out.error and "2 places" in out.error
    assert out.content is None


def test_empty_replace_is_a_deletion():
    current = "keep1\nDELETE ME\nkeep2\n"
    # Delete the middle line entirely, including its trailing newline.
    blocks = [EditBlock(search="DELETE ME\n", replace="")]
    out = apply_search_replace(current, blocks)
    assert out.ok
    assert out.content == "keep1\nkeep2\n"


def test_empty_search_is_rejected():
    out = apply_search_replace("anything", [EditBlock(search="", replace="x")])
    assert not out.ok
    assert "empty SEARCH" in out.error


def test_no_blocks_returns_content_unchanged():
    out = apply_search_replace("unchanged\n", [])
    assert out.ok
    assert out.content == "unchanged\n"


# ── parse_edit_blocks ────────────────────────────────────────────────────────

def test_parse_single_file_single_block():
    text = (
        "### src/App.swift\n"
        "<<<<<<< SEARCH\n"
        "let x = 1\n"
        "=======\n"
        "let x = 2\n"
        ">>>>>>> REPLACE\n"
    )
    blocks, malformed = _blocks(text)
    assert not malformed
    assert list(blocks) == ["src/App.swift"]
    assert blocks["src/App.swift"] == [EditBlock(search="let x = 1", replace="let x = 2")]


def test_parse_multiple_blocks_same_file():
    text = (
        "### a.py\n"
        "<<<<<<< SEARCH\n"
        "first\n"
        "=======\n"
        "FIRST\n"
        ">>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n"
        "second\n"
        "=======\n"
        "SECOND\n"
        ">>>>>>> REPLACE\n"
    )
    blocks, malformed = _blocks(text)
    assert not malformed
    assert len(blocks["a.py"]) == 2


def test_parse_replace_body_may_contain_hash_headings():
    # A `###` inside the REPLACE body is content, not a new file header.
    text = (
        "### doc.md\n"
        "<<<<<<< SEARCH\n"
        "old\n"
        "=======\n"
        "### A Heading In The File\n"
        "new\n"
        ">>>>>>> REPLACE\n"
    )
    blocks, malformed = _blocks(text)
    assert not malformed
    assert list(blocks) == ["doc.md"]
    assert blocks["doc.md"][0].replace == "### A Heading In The File\nnew"


def test_parse_block_without_header_is_malformed():
    text = (
        "<<<<<<< SEARCH\n"
        "x\n"
        "=======\n"
        "y\n"
        ">>>>>>> REPLACE\n"
    )
    blocks, malformed = _blocks(text)
    assert not blocks
    assert malformed and "no `### path` header" in malformed[0]


def test_parse_empty_replace_block():
    text = (
        "### f.txt\n"
        "<<<<<<< SEARCH\n"
        "gone\n"
        "=======\n"
        ">>>>>>> REPLACE\n"
    )
    blocks, malformed = _blocks(text)
    assert not malformed
    assert blocks["f.txt"][0] == EditBlock(search="gone", replace="")


# ── resolve_edits: combine whole-file (new) + applied edits (existing) ────────

def test_new_file_passthrough():
    # A new file (whole-file block) passes through untouched; no edits involved.
    res = resolve_edits(
        whole_files=[("new/module.py", "print('hi')\n")],
        edit_text="",
        existing={},
    )
    assert not res.errors
    assert res.files == [("new/module.py", "print('hi')\n")]


def test_resolve_applies_edit_against_existing_content():
    existing = {"src/App.swift": "let x = 1\nlet y = 2\n"}
    edit_text = (
        "### src/App.swift\n"
        "<<<<<<< SEARCH\n"
        "let x = 1\n"
        "=======\n"
        "let x = 42\n"
        ">>>>>>> REPLACE\n"
    )
    res = resolve_edits(whole_files=[], edit_text=edit_text, existing=existing)
    assert not res.errors
    assert res.files == [("src/App.swift", "let x = 42\nlet y = 2\n")]


def test_resolve_mixed_new_and_existing():
    existing = {"exist.py": "a = 1\n"}
    edit_text = (
        "### exist.py\n"
        "<<<<<<< SEARCH\n"
        "a = 1\n"
        "=======\n"
        "a = 99\n"
        ">>>>>>> REPLACE\n"
    )
    res = resolve_edits(
        whole_files=[("brand_new.py", "b = 2\n")],
        edit_text=edit_text,
        existing=existing,
    )
    assert not res.errors
    got = dict(res.files)
    assert got["brand_new.py"] == "b = 2\n"
    assert got["exist.py"] == "a = 99\n"


def test_resolve_unappliable_edit_becomes_error_and_no_write():
    existing = {"exist.py": "a = 1\n"}
    edit_text = (
        "### exist.py\n"
        "<<<<<<< SEARCH\n"
        "this line is not in the file\n"
        "=======\n"
        "whatever\n"
        ">>>>>>> REPLACE\n"
    )
    res = resolve_edits(whole_files=[], edit_text=edit_text, existing=existing)
    assert res.errors
    assert "exist.py" in res.errors[0]
    # The file is NOT in the resolved set — nothing gets written on failure.
    assert "exist.py" not in dict(res.files)


def test_resolve_edit_for_unknown_file_is_error():
    # Model emitted edits for a file we never showed it → no base to apply to.
    edit_text = (
        "### mystery.py\n"
        "<<<<<<< SEARCH\n"
        "x\n"
        "=======\n"
        "y\n"
        ">>>>>>> REPLACE\n"
    )
    res = resolve_edits(whole_files=[], edit_text=edit_text, existing={})
    assert res.errors
    assert "mystery.py" in res.errors[0]
    assert "not among the existing files" in res.errors[0]
