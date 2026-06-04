"""Behavioural tests for the daemon's manifest-mode orchestration (#4).

Exercise _generate_via_manifest / _generate_implementation with call_agent
mocked, against a throwaway DB. No network, no spec-dir reads (spec_md/design_md
are passed in; generation returns content, the caller writes it).
"""
import json
from unittest import mock

import pytest

import qwen_server.orchestrator_daemon as d
from qwen_autonomous import executor
from qwen_autonomous.db import Database
from qwen_autonomous.executor import ImplementerResult, ParseError


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
    return db.get_spec(spec.id), db.get_task(task.id), db.spec_dir(spec.id)


MANIFEST = ("<<<MANIFEST>>>\n"
            "shared/t.ts | contract types | T\n"
            "client/m.ts | entry point |\n"
            "<<<END_MANIFEST>>>")
FILE_T = "<<<FILE: shared/t.ts>>>\nexport type T = number;\n<<<END_FILE>>>"
FILE_M = "<<<FILE: client/m.ts>>>\nimport { T } from './t';\nconst x: T = 1;\n<<<END_FILE>>>"


def test_manifest_mode_generates_each_file_in_order(db, spec_task):
    spec, task, spec_dir = spec_task
    with mock.patch.object(d, "call_agent", side_effect=[MANIFEST, FILE_T, FILE_M]) as ca:
        res = d._generate_via_manifest(db, spec, task, spec_dir, "SPEC", "DESIGN",
                                       "implementer", [], None)
    assert isinstance(res, ImplementerResult)
    assert [p for p, _ in res.files] == ["shared/t.ts", "client/m.ts"]
    assert "export type T" in dict(res.files)["shared/t.ts"]
    assert "import { T }" in dict(res.files)["client/m.ts"]
    # 1 manifest call + 1 per file
    assert ca.call_count == 3


def test_manifest_enforces_canonical_path_over_model_drift(db, spec_task):
    # Model returns the right content but under a slightly different path; the
    # orchestrator must key it to the manifest path.
    spec, task, spec_dir = spec_task
    drifted = "<<<FILE: ./shared/t.ts>>>\nexport type T = string;\n<<<END_FILE>>>"
    one = "<<<MANIFEST>>>\nshared/t.ts | types | T\n<<<END_MANIFEST>>>"
    with mock.patch.object(d, "call_agent", side_effect=[one, drifted]):
        res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                       "implementer", [], None)
    assert [p for p, _ in res.files] == ["shared/t.ts"]


def test_manifest_parse_failure_propagates_as_parse_error(db, spec_task):
    spec, task, spec_dir = spec_task
    with mock.patch.object(d, "call_agent", side_effect=["no markers at all"]):
        res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                       "implementer", [], None)
    assert isinstance(res, ParseError)


def test_per_file_retry_then_success(db, spec_task):
    # First per-file attempt is unparseable; the retry yields a valid block.
    spec, task, spec_dir = spec_task
    one = "<<<MANIFEST>>>\nshared/t.ts | types | T\n<<<END_MANIFEST>>>"
    bad = "I forgot the file markers, sorry"
    good = FILE_T
    with mock.patch.object(executor, "PER_FILE_PARSE_RETRIES", 2):
        with mock.patch.object(d, "call_agent", side_effect=[one, bad, good]) as ca:
            res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                           "implementer", [], None)
    assert isinstance(res, ImplementerResult)
    assert [p for p, _ in res.files] == ["shared/t.ts"]
    assert ca.call_count == 3  # manifest + 1 failed + 1 retry


def test_dispatch_single_vs_manifest(db, spec_task):
    spec, task, spec_dir = spec_task
    small = "## Files\nonly/one.ts"
    big = "## Files\n" + "\n".join(f"d/f{i}.ts" for i in range(20))

    # single-call path: one implementer call returns a FILE block
    with mock.patch.object(executor, "IMPLEMENTER_MODE", "auto"):
        with mock.patch.object(d, "call_agent",
                               side_effect=["<<<FILE: only/one.ts>>>\nx\n<<<END_FILE>>>"]) as ca:
            res = d._generate_implementation(db, spec, task, spec_dir, "S", small,
                                             "implementer", [], None)
        assert isinstance(res, ImplementerResult)
        assert ca.call_count == 1  # single shot

    # manifest path: manifest + per-file
    mani = "<<<MANIFEST>>>\n" + "\n".join(f"d/f{i}.ts | f{i} |" for i in range(20)) + "\n<<<END_MANIFEST>>>"
    files = [f"<<<FILE: d/f{i}.ts>>>\ncontent{i}\n<<<END_FILE>>>" for i in range(20)]
    with mock.patch.object(executor, "IMPLEMENTER_MODE", "auto"):
        with mock.patch.object(d, "call_agent", side_effect=[mani, *files]) as ca:
            res = d._generate_implementation(db, spec, task, spec_dir, "S", big,
                                             "implementer", [], None)
        assert isinstance(res, ImplementerResult)
        assert len(res.files) == 20
        assert ca.call_count == 21  # 1 manifest + 20 files


# ── #4b: targeted retries (regenerate only reviewer-cited files) ──────────────

def _seed_prior_snapshot(spec_dir, files):
    """Write a retry_history/retry_0 snapshot: manifest.json + the given files."""
    snap = spec_dir / "retry_history" / "retry_0"
    snap.mkdir(parents=True, exist_ok=True)
    manifest = [{"path": p, "purpose": "x", "exports": ""} for p, _ in files]
    (snap / "manifest.json").write_text(json.dumps(manifest))
    for p, content in files:
        fp = snap / p
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)


def _retry(db, task):
    db.increment_task_retry(task.id)
    return db.get_task(task.id)


def test_targeted_retry_regenerates_only_cited(db, spec_task):
    spec, task, spec_dir = spec_task
    _seed_prior_snapshot(spec_dir, [
        ("shared/t.ts", "export type T = number; // OLD"),
        ("client/m.ts", "import { T } from './t'; // OLD"),
    ])
    task = _retry(db, task)  # retry_count -> 1
    rej = "### Verdict Evidence\n- major client/m.ts:1 - broken import"
    new_m = "<<<FILE: client/m.ts>>>\nimport { T } from './t'; // FIXED\n<<<END_FILE>>>"
    with mock.patch.object(d, "call_agent", side_effect=[new_m]) as ca:
        res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                       "implementer", [], rej)
    assert isinstance(res, ImplementerResult)
    fm = dict(res.files)
    assert "FIXED" in fm["client/m.ts"]            # cited file regenerated
    assert "OLD" in fm["shared/t.ts"]              # uncited file reused from snapshot
    assert ca.call_count == 1                      # ONLY the cited file — no manifest call
    assert (spec_dir / "manifest.json").exists()   # re-persisted for the next retry


def test_targeted_retry_falls_back_to_full_when_no_snapshot(db, spec_task):
    spec, task, spec_dir = spec_task
    task = _retry(db, task)  # retry 1, but no retry_history snapshot exists
    with mock.patch.object(d, "call_agent", side_effect=[MANIFEST, FILE_T, FILE_M]) as ca:
        res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                       "implementer", [], "- client/m.ts:1 - broken")
    assert isinstance(res, ImplementerResult)
    assert ca.call_count == 3  # full regen: manifest + 2 files


def test_targeted_retry_falls_back_when_nothing_cited(db, spec_task):
    spec, task, spec_dir = spec_task
    _seed_prior_snapshot(spec_dir, [("shared/t.ts", "old"), ("client/m.ts", "old")])
    task = _retry(db, task)
    with mock.patch.object(d, "call_agent", side_effect=[MANIFEST, FILE_T, FILE_M]) as ca:
        res = d._generate_via_manifest(db, spec, task, spec_dir, "S", "D",
                                       "implementer", [], "Generic failure, no file paths.")
    assert isinstance(res, ImplementerResult)
    assert ca.call_count == 3  # full regen (nothing matched the manifest)


def test_parse_cited_paths_full_and_basename():
    known = {"App/server/resolver.ts", "App/client/probe.ts", "shared/t.ts"}
    notes = ("### Verdict Evidence\n- resolver.ts:129 - as any\n"
             "- App/client/probe.ts:7 - any\nunrelated note about anything else")
    cited = d._parse_cited_paths(notes, known)
    assert cited == {"App/server/resolver.ts", "App/client/probe.ts"}
    assert "shared/t.ts" not in cited  # 'anything' must not match 't.ts'
