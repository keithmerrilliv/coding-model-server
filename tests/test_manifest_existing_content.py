"""DEV-604 — manifest mode must receive and preserve existing-file content.

Runs 18 and 19 regenerated existing files whole from priors: the fetch that
DEV-571 armed for the single-call path was silently disarmed on the manifest
path (its extra-paths filter expected dicts and entries are ManifestEntry
dataclasses), so every per-file call got existing_content=None and the model
reconstructed each file from the design — gutting a 117-line class to 33 lines
in run 18 and emitting 43 lines of a 5,804-line file in run 19.

Four halves pinned here:
  * the manifest's own paths arm the existing-file fetch;
  * the per-file prompt asks for SEARCH/REPLACE edit blocks (DEV-581) when the
    target exists and the flag is on, byte-identically otherwise;
  * _generate_one_file applies edit blocks against the current content and
    never overwrites on an anchor that does not apply;
  * manifest entries the approved plan never declared are dropped, and an
    oversized existing file is refused rather than re-emitted whole.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import (
    ImplementerResult,
    ManifestEntry,
    build_per_file_message,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer",
                          title="impl demo")
    return db.get_spec(spec.id), db.get_task(task.id)


ENTRIES = [
    ManifestEntry(path="game.js", purpose="Pure game logic core", exports="step"),
    ManifestEntry(path="main.js", purpose="entry point", exports=""),
]
GAME = "export function step() {}\nexport const SIZE = 3\n"


# ── the fetch is armed by the manifest's own paths ───────────────────────────

def test_manifest_paths_arm_the_existing_file_fetch(db, spec_task):
    """The regression: an isinstance(e, dict) filter emptied extra_paths for
    ManifestEntry lists, so the fetch keyed only on the (optional, brittle)
    change-surface parse and manifest mode ran blind."""
    spec, task = spec_task
    seen = {}

    def fake_fetch(spec_arg, spec_md, extra_paths=()):
        seen["extra_paths"] = list(extra_paths)
        return []

    with mock.patch.object(d, "_fetch_existing_files_for_spec",
                           side_effect=fake_fetch), \
         mock.patch.object(d, "_fetch_protected_files_for_spec",
                           return_value=[]), \
         mock.patch.object(d, "_generate_one_file", return_value="content"):
        d._build_from_manifest(
            db, spec, task, "SPEC", "DESIGN", ENTRIES, "implementer", [],
            None, prior_files=None, only=None, raw="raw")
    assert seen["extra_paths"] == ["game.js", "main.js"]


# ── the per-file prompt ──────────────────────────────────────────────────────

def _texts(messages):
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    return system, user


def test_edit_mode_prompt_asks_for_edit_blocks():
    system, user = _texts(build_per_file_message(
        "spec", "design", ENTRIES, ENTRIES[0], "none",
        existing_content=GAME, edit_mode=True))
    assert "SEARCH/REPLACE" in system
    assert "### game.js" in user            # the required header, spelled out
    assert "Do NOT emit a <<<FILE:" in user
    assert GAME.strip() in user             # the content is still shown


def test_edit_mode_off_is_byte_identical_to_the_legacy_prompt():
    with_flag = build_per_file_message(
        "spec", "design", ENTRIES, ENTRIES[0], "none",
        existing_content=GAME, edit_mode=False)
    system, user = _texts(with_flag)
    assert "SEARCH/REPLACE" not in system
    assert f"Output exactly one <<<FILE: {ENTRIES[0].path}>>>" in user


def test_edit_mode_without_existing_content_changes_nothing():
    """A new file is whole-file emission either way — the flag must not leak."""
    off = build_per_file_message("spec", "design", ENTRIES, ENTRIES[1], "none")
    on = build_per_file_message("spec", "design", ENTRIES, ENTRIES[1], "none",
                                edit_mode=True)
    assert off == on


def test_edit_errors_are_threaded_into_the_retry_prompt():
    _, user = _texts(build_per_file_message(
        "spec", "design", ENTRIES, ENTRIES[0], "none",
        existing_content=GAME, edit_mode=True,
        edit_errors="- edit block #1: SEARCH text not found"))
    assert "previous edit blocks failed to apply" in user
    assert "SEARCH text not found" in user


# ── _generate_one_file applies edits ─────────────────────────────────────────

EDIT_RESPONSE = """### game.js
<<<<<<< SEARCH
export function step() {}
=======
export function step() { return 1 }
>>>>>>> REPLACE
"""

BROKEN_EDIT_RESPONSE = """### game.js
<<<<<<< SEARCH
this text is not in the file
=======
whatever
>>>>>>> REPLACE
"""


def _gen(db, spec, task, response_or_responses, existing=GAME):
    responses = (response_or_responses
                 if isinstance(response_or_responses, list)
                 else [response_or_responses])
    calls = []

    def fake_call_agent(role, messages, **kwargs):
        calls.append(messages)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    with mock.patch.object(executor, "DIFF_BASED_EDITS", True), \
         mock.patch.object(executor, "PER_FILE_PARSE_RETRIES", 1), \
         mock.patch.object(d, "call_agent", side_effect=fake_call_agent):
        content = d._generate_one_file(
            db, spec, task, "SPEC", "DESIGN", ENTRIES, ENTRIES[0], [],
            "implementer", [], None,
            existing_by_path={"game.js": existing})
    return content, calls


def test_valid_edit_blocks_are_applied_to_the_current_content(db, spec_task):
    spec, task = spec_task
    content, calls = _gen(db, spec, task, EDIT_RESPONSE)
    assert content == GAME.replace(
        "export function step() {}", "export function step() { return 1 }")
    assert len(calls) == 1


def test_unappliable_anchor_never_overwrites_and_threads_diagnostics(db, spec_task):
    spec, task = spec_task
    content, calls = _gen(db, spec, task, BROKEN_EDIT_RESPONSE)
    assert content is None, "a SEARCH that does not apply must not produce a file"
    assert len(calls) == 2, "the failure is retried, not accepted"
    _, retry_user = _texts(calls[1])
    assert "previous edit blocks failed to apply" in retry_user


def test_second_attempt_can_recover_from_a_bad_first_anchor(db, spec_task):
    spec, task = spec_task
    content, calls = _gen(db, spec, task,
                          [BROKEN_EDIT_RESPONSE, EDIT_RESPONSE])
    assert content is not None and "return 1" in content
    assert len(calls) == 2


def test_oversized_existing_file_is_refused_without_edit_mode(db, spec_task):
    """Whole-file re-emission of a large file ships fragments (runs 18/19) —
    refuse loudly instead of generating blind."""
    spec, task = spec_task
    big = "x = 1\n" * 20_000  # > MANIFEST_WHOLE_FILE_MAX_CHARS
    with mock.patch.object(executor, "DIFF_BASED_EDITS", False), \
         mock.patch.object(d, "call_agent") as agent:
        content = d._generate_one_file(
            db, spec, task, "SPEC", "DESIGN", ENTRIES, ENTRIES[0], [],
            "implementer", [], None,
            existing_by_path={"game.js": big})
    assert content is None
    agent.assert_not_called()


def test_small_existing_file_still_regenerates_whole_without_edit_mode(db, spec_task):
    spec, task = spec_task
    whole = f"<<<FILE: game.js>>>\n{GAME}<<<END_FILE>>>"
    with mock.patch.object(executor, "DIFF_BASED_EDITS", False), \
         mock.patch.object(d, "call_agent", return_value=whole):
        content = d._generate_one_file(
            db, spec, task, "SPEC", "DESIGN", ENTRIES, ENTRIES[0], [],
            "implementer", [], None,
            existing_by_path={"game.js": GAME})
    assert content is not None and content.strip() == GAME.strip()


def test_oversized_whole_file_reemission_in_edit_mode_is_rejected(db, spec_task):
    """Rule 5 violations on a file no output budget can carry must retry, not
    pass through — passing through is the fragment factory itself."""
    spec, task = spec_task
    big = "x = 1\n" * 20_000
    fragment = "<<<FILE: game.js>>>\nx = 1\n<<<END_FILE>>>"
    content, calls = _gen(db, spec, task, fragment, existing=big)
    assert content is None
    assert len(calls) == 2
    _, retry_user = _texts(calls[1])
    assert "re-emitted" in retry_user


# ── undeclared manifest entries are dropped ──────────────────────────────────

PLAN_YAML = """
title: demo
phases:
  - name: implement
    outputs: [game.js, main.js]
"""


def _spec_with_plan(yaml_text):
    return SimpleNamespace(id="spec_test", normalized_yaml=yaml_text)


def test_undeclared_manifest_entries_are_dropped():
    entries = ENTRIES + [ManifestEntry(path="Invented.swift", purpose="junk")]
    kept = d._drop_undeclared_manifest_entries(_spec_with_plan(PLAN_YAML), entries)
    assert [e.path for e in kept] == ["game.js", "main.js"]


def test_plan_without_outputs_constrains_nothing():
    entries = ENTRIES + [ManifestEntry(path="Extra.js", purpose="helper")]
    kept = d._drop_undeclared_manifest_entries(
        _spec_with_plan("title: demo\nphases:\n  - name: implement\n"), entries)
    assert kept == entries


def test_zero_matches_keeps_the_manifest_rather_than_emptying_it():
    """Wholesale path drift (DEV-601) must not turn into a no-file build."""
    entries = [ManifestEntry(path="src/game.js", purpose="drifted prefix")]
    kept = d._drop_undeclared_manifest_entries(_spec_with_plan(PLAN_YAML), entries)
    assert kept == entries
