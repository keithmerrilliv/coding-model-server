"""DEV-581: diff-based edits behind a default-OFF flag.

Covers the wiring around the pure apply_edits module:
  - the implementer prompt gains SEARCH/REPLACE instructions ONLY in edit mode;
  - with the flag OFF the implement path is byte-identical to today;
  - the parser tells edit-block output from whole-file output (and mixed);
  - the single-call generator applies edits (flag ON) and surfaces unappliable
    anchors as apply_errors, which route back to the implementer.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.apply_edits import parse_edit_blocks
from coding_model_autonomous.executor import (
    ImplementerResult,
    ParseError,
    build_implementer_message,
    parse_implementer_response,
)
from coding_model_autonomous.models import GateStatus, GateType, TaskStatus


def _user_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def _system_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


EXISTING = [("src/App.swift", "let x = 1\nlet y = 2\n")]


# ── flag OFF: byte-identical to the pre-DEV-581 whole-file prompt ─────────────

def test_flag_off_prompt_is_byte_identical_with_existing_files():
    """The pin: edit_mode defaulting off must not change a single byte, even
    when there ARE existing files (the case edit mode would otherwise rewrite)."""
    default = build_implementer_message("spec", "design", existing_files=EXISTING)
    explicit_off = build_implementer_message(
        "spec", "design", existing_files=EXISTING, edit_mode=False)
    assert default == explicit_off
    text = _user_text(default) + _system_text(default)
    # Whole-file framing present, edit-block framing absent.
    assert "deleted from the repository" in text
    assert "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for every file" in _user_text(default)
    assert "SEARCH" not in text and "REPLACE" not in text
    # The retry branch is also whole-file when the flag is off.
    retry_off = build_implementer_message(
        "spec", "design", existing_files=EXISTING, rejection_notes="x")
    assert "output ALL files again (complete files, not diffs)" in _user_text(retry_off)


def test_edit_mode_with_no_existing_files_is_byte_identical():
    """No existing files means nothing to edit — edit mode must collapse to the
    whole-file prompt exactly."""
    off = build_implementer_message("spec", "design", existing_files=[])
    on = build_implementer_message("spec", "design", existing_files=[], edit_mode=True)
    assert off == on
    assert "SEARCH" not in (_user_text(on) + _system_text(on))


# ── flag ON: the prompt switches to SEARCH/REPLACE for existing files ─────────

def test_edit_mode_prompt_instructs_search_replace_for_existing_files():
    messages = build_implementer_message(
        "spec", "design", existing_files=EXISTING, edit_mode=True)
    system = _system_text(messages)
    user = _user_text(messages)
    # System prompt carries the exact block delimiters.
    assert "<<<<<<< SEARCH" in system
    assert "=======" in system
    assert ">>>>>>> REPLACE" in system
    assert "Editing existing files" in system
    # New files still get whole-file emission instructions.
    assert "<<<FILE: path>>>" in system
    # The existing-files context tells the model NOT to re-emit them whole.
    assert "do NOT re-emit" in user or "do NOT re-emit it whole" in user
    # The task line points at edit blocks, not "output ALL files".
    assert "output ALL files again (complete files, not diffs)" not in user


def test_edit_mode_retry_task_line_points_at_edits():
    messages = build_implementer_message(
        "spec", "design", existing_files=EXISTING, edit_mode=True,
        rejection_notes="fix the thing")
    user = _user_text(messages)
    assert "fix the thing" in user
    assert "SEARCH/REPLACE edit blocks" in user


# ── parser: edit-block vs whole-file vs mixed ────────────────────────────────

WHOLE = "<<<FILE: new/mod.py>>>\nprint('x')\n<<<END_FILE>>>"
EDIT = (
    "### src/App.swift\n"
    "<<<<<<< SEARCH\n"
    "let x = 1\n"
    "=======\n"
    "let x = 9\n"
    ">>>>>>> REPLACE\n"
)


def test_parser_detects_whole_file_output():
    parsed = parse_implementer_response(WHOLE)
    assert isinstance(parsed, ImplementerResult)
    assert parsed.files == [("new/mod.py", "print('x')")]
    # No SEARCH/REPLACE edit blocks in whole-file output.
    assert parse_edit_blocks(WHOLE).is_empty()


def test_parser_detects_edit_block_output():
    # Pure edit output has no <<<FILE>>> blocks → ParseError from the whole-file
    # parser, but parse_edit_blocks recovers the per-file edits.
    assert isinstance(parse_implementer_response(EDIT), ParseError)
    edits = parse_edit_blocks(EDIT)
    assert [fe.path for fe in edits.files] == ["src/App.swift"]


def test_parser_handles_mixed_output():
    mixed = WHOLE + "\n\n" + EDIT
    whole = parse_implementer_response(mixed)
    assert isinstance(whole, ImplementerResult)
    assert whole.files == [("new/mod.py", "print('x')")]  # only the new file
    edits = parse_edit_blocks(mixed)
    assert [fe.path for fe in edits.files] == ["src/App.swift"]  # only the existing


# ── single-call generation: flag ON applies edits ───────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer",
                          title="impl")
    return db.get_spec(spec.id), db.get_task(task.id), db.spec_dir(spec.id)


SMALL_DESIGN = "## Files\nsrc/App.swift"


def _patch_context(existing):
    """Patch the two runner-backed context fetches for a single-call run."""
    return (
        mock.patch.object(d, "_fetch_existing_files_for_spec", return_value=existing),
        mock.patch.object(d, "_fetch_protected_files_for_spec", return_value=[]),
    )


def test_single_call_applies_edits_when_flag_on(db, spec_task):
    spec, task, spec_dir = spec_task
    model_out = (
        "### src/App.swift\n"
        "<<<<<<< SEARCH\n"
        "let x = 1\n"
        "=======\n"
        "let x = 100\n"
        ">>>>>>> REPLACE\n"
    )
    p_exist, p_prot = _patch_context(list(EXISTING))
    with mock.patch.object(executor, "DIFF_BASED_EDITS", True), p_exist, p_prot, \
            mock.patch.object(d, "call_agent", return_value=model_out):
        res = d._generate_implementation(db, spec, task, spec_dir, "S",
                                         SMALL_DESIGN, "implementer", [], None)
    assert isinstance(res, ImplementerResult)
    assert not res.apply_errors
    assert dict(res.files)["src/App.swift"] == "let x = 100\nlet y = 2\n"


def test_single_call_surfaces_unappliable_anchor_as_apply_error(db, spec_task):
    spec, task, spec_dir = spec_task
    model_out = (
        "### src/App.swift\n"
        "<<<<<<< SEARCH\n"
        "let x = DOES NOT MATCH\n"
        "=======\n"
        "let x = 100\n"
        ">>>>>>> REPLACE\n"
    )
    p_exist, p_prot = _patch_context(list(EXISTING))
    with mock.patch.object(executor, "DIFF_BASED_EDITS", True), p_exist, p_prot, \
            mock.patch.object(d, "call_agent", return_value=model_out):
        res = d._generate_implementation(db, spec, task, spec_dir, "S",
                                         SMALL_DESIGN, "implementer", [], None)
    assert isinstance(res, ImplementerResult)
    assert res.apply_errors
    assert "src/App.swift" in res.apply_errors[0]


def test_single_call_flag_off_never_applies_edits(db, spec_task):
    """The flag-off pin at the generator level: even with existing files and an
    edit-shaped response, nothing is applied — the whole-file parser runs and a
    bare edit response is simply unparseable (today's behaviour)."""
    spec, task, spec_dir = spec_task
    model_out = EDIT
    p_exist, p_prot = _patch_context(list(EXISTING))
    with mock.patch.object(executor, "DIFF_BASED_EDITS", False), p_exist, p_prot, \
            mock.patch.object(d, "call_agent", return_value=model_out) as ca:
        res = d._generate_implementation(db, spec, task, spec_dir, "S",
                                         SMALL_DESIGN, "implementer", [], None)
    # Whole-file parser: no <<<FILE>>> blocks → ParseError, exactly as before.
    assert isinstance(res, ParseError)
    # The prompt built for the model carried NO edit instructions.
    sent = ca.call_args.args[1]
    assert "SEARCH" not in _system_text(sent)


def test_single_call_flag_off_whole_file_passthrough(db, spec_task):
    """With the flag off, a normal whole-file response resolves to files with no
    apply_errors — identical to pre-DEV-581."""
    spec, task, spec_dir = spec_task
    p_exist, p_prot = _patch_context(list(EXISTING))
    with mock.patch.object(executor, "DIFF_BASED_EDITS", False), p_exist, p_prot, \
            mock.patch.object(d, "call_agent",
                              return_value="<<<FILE: src/App.swift>>>\nlet x = 5\n<<<END_FILE>>>"):
        res = d._generate_implementation(db, spec, task, spec_dir, "S",
                                         SMALL_DESIGN, "implementer", [], None)
    assert isinstance(res, ImplementerResult)
    assert not res.apply_errors
    assert dict(res.files)["src/App.swift"] == "let x = 5"


# ── routing: unappliable edits go back to the implementer, no silent drop ─────

def test_unappliable_edits_route_to_implementer(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer",
                          title="impl")
    task = db.get_task(task.id)
    errors = ["`src/App.swift`: edit block #1: SEARCH text not found in the current file."]

    d._route_unappliable_edits(db, db.get_spec(spec.id), task, errors)

    # A REJECTED code_review gate carries the diagnostic (same channel a build
    # failure uses), and the task is requeued for another implementer attempt.
    gates = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)
    assert gates and gates[-1].status is GateStatus.REJECTED
    assert "could not be applied" in gates[-1].reviewer_notes
    assert "src/App.swift" in gates[-1].reviewer_notes
    reloaded = db.get_task(task.id)
    assert reloaded.status is TaskStatus.PENDING
    assert reloaded.retry_count == 1


def test_reemit_instruction_respects_flag():
    with mock.patch.object(executor, "DIFF_BASED_EDITS", False):
        assert d._reemit_instruction("re-emit ALL files.") == "re-emit ALL files."
    with mock.patch.object(executor, "DIFF_BASED_EDITS", True):
        out = d._reemit_instruction("re-emit ALL files.")
        assert "SEARCH/REPLACE" in out and "ALL files" not in out
