"""Behavioural tests for the daemon's manifest-mode orchestration (#4).

Exercise _generate_via_manifest / _generate_implementation with call_agent
mocked, against a throwaway DB. No network, no spec-dir reads (spec_md/design_md
are passed in; generation returns content, the caller writes it).
"""
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
