"""DEV-638: the anchored-edit applier is a match ladder, not a byte-exact gate.

Run 21 spent eleven implementer rotations landing nothing: six anchors that
missed byte-for-byte on a 6,130-line file, five SEARCH/REPLACE blocks aimed at
a file that did not exist yet. The ladder — exact, trailing whitespace,
indent-relative, unique high-similarity window — lands a transcription slip
and still refuses anything that matches twice; the prompt names each planned
path's mandatory form; a lone empty-SEARCH block for a new path is taken as
the whole file; every non-exact apply is recorded on the event and shown on
the gates.
"""
import json
import time
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import apply_edits, executor
from coding_model_autonomous.apply_edits import (
    EditBlock,
    apply_search_replace,
    resolve_edits,
)
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult, build_implementer_message
from coding_model_autonomous.models import EventKind, GateType, TaskStatus


def _lines(*ls):
    return "\n".join(ls)


# ── tier 1 (exact) is unchanged ───────────────────────────────────────────────

def test_exact_tier_keeps_substring_semantics_and_reports_exact():
    out = apply_search_replace("ab\ncd\n", [EditBlock("b\nc", "X")])
    assert out.ok and out.content == "aXd\n"          # substring, as before DEV-638
    assert [(a.tier, a.line) for a in out.applied] == [(apply_edits.TIER_EXACT, None)]


# ── tier 2: trailing whitespace ───────────────────────────────────────────────

def test_trailing_whitespace_slip_applies_and_preserves_other_lines():
    current = "def f():   \n    return 1  \n\nother = 2 \n"
    out = apply_search_replace(current, [EditBlock("def f():\n    return 1",
                                                   "def f():\n    return 2")])
    assert out.ok
    assert out.content == "def f():\n    return 2\n\nother = 2 \n"   # untouched line keeps its space
    assert out.applied[0].tier == apply_edits.TIER_TRAILING_WS
    assert out.applied[0].line == 1


def test_trailing_whitespace_tier_refuses_when_two_windows_match():
    current = "x = 1 \ny\nz\nx = 1  \ny\n"
    out = apply_search_replace(current, [EditBlock("x = 1\ny", "x = 2\ny")])
    assert not out.ok and out.reason == "ambiguous"
    assert "trailing_ws" in out.error


def test_empty_replace_deletes_through_a_tolerant_tier():
    current = "keep\ndrop me \ndrop too\nend\n"
    out = apply_search_replace(current, [EditBlock("drop me\ndrop too", "")])
    assert out.ok and out.content == "keep\nend\n"
    assert out.applied[0].tier == apply_edits.TIER_TRAILING_WS


# ── tier 3: indent-relative ───────────────────────────────────────────────────

CLASS_BODY = _lines(
    "class A:",
    "    def m(self):",
    "        if x:",
    "            return 1",
    "        return 0",
    "",
)


def test_indent_slip_applies_with_replace_reindented_to_the_window():
    # The model copied the block at 4 spaces shallower than it really sits.
    search = _lines("    if x:", "        return 1", "    return 0")
    replace = _lines("    if x:", "        return 2", "        # nested", "    return 0")
    out = apply_search_replace(CLASS_BODY, [EditBlock(search, replace)])
    assert out.ok
    assert out.content == _lines(
        "class A:", "    def m(self):", "        if x:", "            return 2",
        "            # nested", "        return 0", "")
    assert out.applied[0].tier == apply_edits.TIER_INDENT
    assert out.applied[0].line == 3


def test_indent_tier_reindents_a_zero_indent_anchor():
    search = _lines("if x:", "    return 1", "return 0")
    out = apply_search_replace(CLASS_BODY, [EditBlock(search, "pass")])
    assert out.ok and "        pass\n" in out.content
    assert out.applied[0].tier == apply_edits.TIER_INDENT


def test_indent_tier_refuses_duplicates():
    current = _lines("    a", "    b", "        a", "        b", "")
    out = apply_search_replace(current, [EditBlock("a\nb", "c")])
    assert not out.ok and out.reason == "ambiguous" and "indent" in out.error


# ── tier 4: unique similarity window ──────────────────────────────────────────

def _body(n, tag=""):
    return [f"    value_{i}{tag} = compute_{i}(alpha, beta_{i}) + {i}" for i in range(n)]


def test_one_slipped_token_in_a_ten_line_anchor_lands_by_similarity():
    real = _body(10)
    current = _lines("head = 0", *real, "tail = 1", "")
    slipped = list(real)
    slipped[4] = slipped[4].replace("beta_4", "beta4")     # a dropped underscore
    out = apply_search_replace(current, [EditBlock(_lines(*slipped), "    replaced = True")])
    assert out.ok, out.error
    assert out.content == _lines("head = 0", "    replaced = True", "tail = 1", "")
    a = out.applied[0]
    assert a.tier == apply_edits.TIER_FUZZY and a.ratio >= 0.95 and a.line == 2


def test_a_dropped_line_in_a_long_anchor_matches_the_longer_window():
    real = _body(9)
    current = _lines("head", *real, "tail", "")
    dropped = real[:4] + real[5:]                            # model skipped line 5
    out = apply_search_replace(current, [EditBlock(_lines(*dropped), "    x = 1")])
    assert out.ok, out.error
    assert out.content == _lines("head", "    x = 1", "tail", "")
    assert out.applied[0].tier == apply_edits.TIER_FUZZY


def test_far_off_anchor_refuses_and_names_the_closest_window():
    real = _body(8)
    current = _lines(*real, "")
    wrong = [ln.replace("compute", "different") for ln in real]   # ~0.85 similar
    out = apply_search_replace(current, [EditBlock(_lines(*wrong), "x")])
    assert not out.ok and out.reason == "not_found"
    assert "Closest window" in out.error and "similarity" in out.error


def test_two_equally_close_regions_are_ambiguous():
    region = _body(6)
    current = _lines(*region, "gap = 1", "gap = 2", *region, "")
    slipped = list(region)
    slipped[2] = slipped[2].replace("alpha", "alphaa")
    out = apply_search_replace(current, [EditBlock(_lines(*slipped), "x")])
    assert not out.ok and out.reason == "ambiguous" and "fuzzy" in out.error


def test_a_clearly_better_region_wins_over_a_distant_second():
    real = _body(8)
    other = [ln.replace("compute", "consume").replace("alpha", "omega") for ln in real]
    current = _lines(*other, "gap", *real, "")
    slipped = list(real)
    slipped[1] = slipped[1].replace("beta_1", "beta1")
    out = apply_search_replace(current, [EditBlock(_lines(*slipped), "    won = 1")])
    assert out.ok, out.error
    assert out.content == _lines(*other, "gap", "    won = 1", "")


def test_similarity_tier_is_not_attempted_for_tiny_anchors():
    current = "alpha = 1\nbeta = 2\n"
    out = apply_search_replace(current, [EditBlock("alpha = 1\nbeta = 3", "x")])
    assert not out.ok and out.reason == "not_found"
    assert "Closest window" not in out.error


def test_large_files_require_the_stricter_threshold():
    real = _body(9)
    slipped = real[:4] + real[5:]                       # one dropped line: ~0.94
    small = _lines(*real, "")
    big = _lines(*(["filler = 0"] * 2100), *real, "")
    assert apply_search_replace(small, [EditBlock(_lines(*slipped), "x")]).ok
    out = apply_search_replace(big, [EditBlock(_lines(*slipped), "x")])
    assert not out.ok and out.reason == "not_found"


def test_ladder_is_fast_enough_on_a_daemon_sized_file():
    filler = [f"    line_{i} = f_{i}(a, b) + {i}" for i in range(6000)]
    real = _body(30, tag="_real")
    current = _lines(*filler[:3000], *real, *filler[3000:], "")
    slipped = list(real)
    slipped[10] = slipped[10].replace("alpha", "alpha_")
    t0 = time.monotonic()
    out = apply_search_replace(current, [EditBlock(_lines(*slipped), "    ok = 1")])
    assert out.ok, out.error
    assert time.monotonic() - t0 < 5.0
    assert out.applied[0].tier == apply_edits.TIER_FUZZY


# ── sequential blocks and line-ending preservation ────────────────────────────

def test_blocks_apply_sequentially_across_tiers():
    current = "a = 1\nb = 2 \nc = 3\n"
    out = apply_search_replace(current, [EditBlock("a = 1", "a = 10"),
                                         EditBlock("b = 2\nc = 3", "b = 20\nc = 3")])
    assert out.ok and out.content == "a = 10\nb = 20\nc = 3\n"
    assert [a.tier for a in out.applied] == [apply_edits.TIER_EXACT,
                                             apply_edits.TIER_TRAILING_WS]


def test_tolerant_tiers_keep_the_files_trailing_newline_state():
    with_nl = apply_search_replace("x = 1 \ny\n", [EditBlock("x = 1\ny", "x = 2\ny")])
    without = apply_search_replace("x = 1 \ny", [EditBlock("x = 1\ny", "x = 2\ny")])
    assert with_nl.content == "x = 2\ny\n" and without.content == "x = 2\ny"


# ── resolve_edits: applied list and new-path handling ─────────────────────────

def test_resolve_reports_every_applied_block_with_its_tier():
    text = ("### src/app.py\n<<<<<<< SEARCH\na = 1\n=======\na = 2\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nb = 2\nc\n=======\nb = 3\nc\n>>>>>>> REPLACE\n")
    res = resolve_edits([], text, {"src/app.py": "a = 1\nb = 2 \nc\n"})
    assert not res.errors
    assert [(a.path, a.block, a.tier) for a in res.applied] == [
        ("src/app.py", 1, apply_edits.TIER_EXACT),
        ("src/app.py", 2, apply_edits.TIER_TRAILING_WS)]


def test_lone_empty_search_for_a_new_path_becomes_the_whole_file():
    text = "### tests/test_new.py\n<<<<<<< SEARCH\n=======\ndef test_x():\n    pass\n>>>>>>> REPLACE\n"
    res = resolve_edits([], text, {})
    assert not res.errors
    assert dict(res.files)["tests/test_new.py"] == "def test_x():\n    pass"
    assert res.applied[0].tier == apply_edits.TIER_WHOLE_FROM_EMPTY_SEARCH


def test_edit_blocks_for_a_new_path_say_emit_whole():
    text = "### tests/test_new.py\n<<<<<<< SEARCH\nimport x\n=======\nimport y\n>>>>>>> REPLACE\n"
    res = resolve_edits([], text, {})
    assert res.failures[0].reason == "no_base"
    assert "EMIT WHOLE" in res.errors[0] and "<<<FILE: tests/test_new.py>>>" in res.errors[0]


def test_two_blocks_for_a_new_path_are_refused_even_if_one_is_empty():
    text = ("### tests/test_new.py\n<<<<<<< SEARCH\n=======\nA\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nA\n=======\nB\n>>>>>>> REPLACE\n")
    res = resolve_edits([], text, {})
    assert res.errors and not res.files


# ── prompt: per-path mandatory modes ──────────────────────────────────────────

EXISTING = [("src/App.swift", "let x = 1\n")]


def _user(msgs):
    return "\n".join(m["content"] for m in msgs if m["role"] == "user")


def test_edit_mode_prompt_lists_each_paths_mandatory_form():
    msgs = build_implementer_message("S", "D", existing_files=EXISTING, edit_mode=True,
                                     new_files=["tests/test_x.py", "src/App.swift"])
    text = _user(msgs)
    assert "## File modes — MANDATORY" in text
    assert "EDIT ONLY — existing: `src/App.swift`" in text
    assert "EMIT WHOLE — new file: `tests/test_x.py`" in text
    assert text.count("EDIT ONLY — existing: `src/App.swift`") == 1
    assert "EMIT WHOLE — new file: `src/App.swift`" not in text        # existing wins


def test_file_modes_never_render_outside_edit_mode():
    off = build_implementer_message("S", "D", existing_files=EXISTING, edit_mode=False)
    off_with = build_implementer_message("S", "D", existing_files=EXISTING, edit_mode=False,
                                         new_files=["tests/test_x.py"])
    assert off == off_with
    assert "File modes" not in _user(off_with)
    none_existing = build_implementer_message("S", "D", edit_mode=True, new_files=["a.py"])
    assert "File modes" not in _user(none_existing)


# ── daemon wiring ─────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _spec_with_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Spec")
    (spec_dir / "design.md").write_text("# Design")
    task = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="impl")
    db.update_task_status(task.id, TaskStatus.RUNNING)
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def test_generate_implementation_records_the_tier_and_names_new_paths(db):
    spec, task, spec_dir = _spec_with_task(db)
    model_out = ("### src/App.swift\n<<<<<<< SEARCH\nlet x = 1\nlet y = 2\n=======\n"
                 "let x = 2\nlet y = 2\n>>>>>>> REPLACE\n")
    with mock.patch.object(executor, "DIFF_BASED_EDITS", True), \
            mock.patch.object(d, "_fetch_existing_files_for_spec",
                              return_value=[("src/App.swift", "let x = 1   \nlet y = 2\n")]), \
            mock.patch.object(d, "_fetch_protected_files_for_spec", return_value=[]), \
            mock.patch.object(d, "_planned_implement_outputs",
                              return_value=["src/App.swift", "tests/test_new.py"]), \
            mock.patch.object(d, "call_agent", return_value=model_out) as ca:
        res = d._generate_implementation(db, spec, task, spec_dir, "S", "## Files\nsrc/App.swift",
                                         "implementer", [], None)
    assert isinstance(res, ImplementerResult) and not res.apply_errors
    assert dict(res.files)["src/App.swift"] == "let x = 2\nlet y = 2\n"
    assert res.edit_applies[0]["tier"] == apply_edits.TIER_TRAILING_WS
    sent = _user(ca.call_args.args[1])
    assert "EMIT WHOLE — new file: `tests/test_new.py`" in sent
    assert "EDIT ONLY — existing: `src/App.swift`" in sent


def test_event_fields_count_tiers_and_list_nonexact():
    res = ImplementerResult(files=[], raw="", edit_applies=[
        {"path": "a", "block": 1, "tier": "exact", "ratio": 1.0, "line": None},
        {"path": "a", "block": 2, "tier": "fuzzy", "ratio": 0.97, "line": 40}])
    fields = d._edit_apply_event_fields(res)
    assert fields["edit_tiers"] == {"exact": 1, "fuzzy": 1}
    assert [a["block"] for a in fields["edit_applies_nonexact"]] == [2]
    assert d._edit_apply_event_fields(ImplementerResult(files=[], raw="")) == {}


def test_tolerant_block_is_empty_for_exact_and_describes_the_rest():
    assert d._tolerant_apply_block([{"path": "a", "block": 1, "tier": "exact"}]) == ""
    block = d._tolerant_apply_block([
        {"path": "a.py", "block": 2, "tier": "fuzzy", "ratio": 0.9612, "line": 40},
        {"path": "b.py", "block": 1, "tier": "indent", "ratio": 1.0, "line": 7}])
    assert "TOLERANT MATCHING" in block
    assert "`a.py` block #2: fuzzy (similarity 0.96) at line 40" in block
    assert "`b.py` block #1: indent at line 7" in block


def test_release_summary_reads_the_latest_implementer_attempt(db):
    spec, task, spec_dir = _spec_with_task(db)
    nonexact = [{"path": "a.py", "block": 1, "tier": "indent", "ratio": 1.0, "line": 3}]
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer", "result_kind": "ImplementerResult",
                             "edit_tiers": {"indent": 1}, "edit_applies_nonexact": nonexact})
    assert "`a.py` block #1: indent at line 3" in d._tolerant_apply_summary_from_events(db, spec.id)
    # A later whole-file attempt carries no tiers: nothing to show.
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer", "result_kind": "ImplementerResult"})
    assert d._tolerant_apply_summary_from_events(db, spec.id) == ""


def test_persisted_response_lists_applied_tiers(db):
    spec, task, spec_dir = _spec_with_task(db)
    res = ImplementerResult(files=[("a.py", "x")], raw="raw", edit_applies=[
        {"path": "a.py", "block": 1, "tier": "fuzzy", "ratio": 0.955, "line": 12}])
    d._persist_implementer_response(spec_dir, task, "deep_implementer", res)
    text = (spec_dir / "implementer_response.md").read_text()
    assert "## Applied edits" in text
    assert "`a.py` block #1: fuzzy (similarity 0.95) at line 12" in text


def test_code_review_gate_names_tolerant_applies(db):
    """The implement-time gate a human reads carries the non-exact applies."""
    spec, task, spec_dir = _spec_with_task(db)
    result = ImplementerResult(
        files=[("src/app.py", "x = 2\n")], raw="",
        edit_applies=[{"path": "src/app.py", "block": 1, "tier": "fuzzy",
                       "ratio": 0.9634, "line": 17}])
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
            mock.patch.object(d, "_fetch_protected_files_for_spec", return_value=[]), \
            mock.patch.object(d, "_load_plan", return_value={}):
        d._run_implementer(db, spec, task, spec_dir)
    gates = [g for g in db.list_gates_for_spec(spec.id) if g.gate_type is GateType.CODE_REVIEW]
    assert gates, "no code_review gate opened"
    prompt = gates[-1].prompt_md
    assert "EDITS APPLIED WITH TOLERANT MATCHING" in prompt
    assert "`src/app.py` block #1: fuzzy (similarity 0.96) at line 17" in prompt
    # and the generation event carries the tier counts
    payloads = [json.loads(e.payload_json)
                for e in db.list_events_by_kind(spec_id=spec.id, kind=EventKind.AGENT_RAN)]
    gen = next(p for p in payloads if p.get("result_kind"))
    assert gen["edit_tiers"] == {"fuzzy": 1}
