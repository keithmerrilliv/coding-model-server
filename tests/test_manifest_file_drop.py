"""DEV-106 — a targeted retry must never leave a manifest-declared file
absent from the live workspace.

spec_54b2c1b3 (2026-07-15): manifest.json declared game.js, the tests import
it, but after a targeted retry it existed only under retry_history/ — so
every test run failed with ERR_MODULE_NOT_FOUND and the retry loop could
never converge.

Two layers of defense:
  * _build_from_manifest falls back to the prior attempt's version when a
    cited file's regeneration fails, instead of dropping it;
  * _verify_manifest_workspace (after the write loop) restores any declared
    file missing from the workspace from the newest snapshot that has it,
    and loudly flags whatever cannot be restored.
"""
import json
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult, ManifestEntry


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


ENTRIES = [
    ManifestEntry(path="game.js", purpose="Pure game logic core", exports="step"),
    ManifestEntry(path="main.js", purpose="entry point", exports=""),
]
PRIOR = {"game.js": "export function step() {}\n", "main.js": "import './game.js';\n"}


def _write_manifest(dirpath, entries):
    dirpath.mkdir(parents=True, exist_ok=True)
    data = [{"path": e.path, "purpose": e.purpose, "exports": e.exports}
            for e in entries]
    (dirpath / "manifest.json").write_text(json.dumps(data))


def test_failed_cited_regeneration_falls_back_to_prior_version(db, spec_task):
    # The reviewer cited main.js; its regeneration fails every parse retry.
    # The stale prior version must ride along rather than vanish.
    spec, task, _spec_dir = spec_task
    with mock.patch.object(d, "_generate_one_file", return_value=None):
        res = d._build_from_manifest(
            db, spec, task, "SPEC", "DESIGN", ENTRIES, "implementer", [],
            "main.js is broken", prior_files=PRIOR, only={"main.js"},
            raw="targeted-retry",
        )
    assert isinstance(res, ImplementerResult)
    files = dict(res.files)
    assert files["game.js"] == PRIOR["game.js"], "uncited file is reused"
    assert files["main.js"] == PRIOR["main.js"], (
        "cited file whose regeneration failed must fall back to the prior "
        "version, not drop out of the workspace"
    )


def test_targeted_retry_end_to_end_keeps_the_file_set_complete(db, spec_task):
    # Through _generate_via_manifest: snapshot present, cited file's
    # regeneration unparseable -> stale fallback, nothing dropped.
    spec, task, spec_dir = spec_task
    snap = spec_dir / "retry_history" / "retry_0"
    _write_manifest(snap, ENTRIES)
    for path, content in PRIOR.items():
        (snap / path).write_text(content)
    db.increment_task_retry(task.id)
    task = db.get_task(task.id)

    with mock.patch.object(executor, "PER_FILE_PARSE_RETRIES", 0), \
         mock.patch.object(d, "call_agent", return_value="no markers, sorry"):
        res = d._generate_via_manifest(
            db, spec, task, spec_dir, "SPEC", "DESIGN", "implementer", [],
            "please fix main.js",
        )
    assert isinstance(res, ImplementerResult)
    assert sorted(p for p, _ in res.files) == ["game.js", "main.js"]


def test_verify_restores_a_missing_declared_file_from_the_newest_snapshot(db, spec_task):
    spec, task, spec_dir = spec_task
    _write_manifest(spec_dir, ENTRIES)
    (spec_dir / "main.js").write_text(PRIOR["main.js"])
    old = spec_dir / "retry_history" / "retry_0"
    old.mkdir(parents=True)
    (old / "game.js").write_text("stale version")
    new = spec_dir / "retry_history" / "retry_1"
    new.mkdir(parents=True)
    (new / "game.js").write_text(PRIOR["game.js"])

    restored, still_missing = d._verify_manifest_workspace(db, spec, task, spec_dir)

    assert restored == ["game.js"]
    assert still_missing == []
    assert (spec_dir / "game.js").read_text() == PRIOR["game.js"], (
        "restoration must prefer the newest snapshot"
    )


def test_verify_flags_an_unrestorable_missing_file(db, spec_task):
    spec, task, spec_dir = spec_task
    _write_manifest(spec_dir, ENTRIES)
    (spec_dir / "main.js").write_text(PRIOR["main.js"])
    # game.js exists nowhere — not in the workspace, no snapshots at all.

    restored, still_missing = d._verify_manifest_workspace(db, spec, task, spec_dir)

    assert restored == []
    assert still_missing == ["game.js"]
    assert not (spec_dir / "game.js").exists()


def test_verify_is_a_noop_without_manifest_json(db, spec_task):
    # Single-call mode never persists manifest.json; nothing to verify.
    spec, task, spec_dir = spec_task
    spec_dir.mkdir(parents=True, exist_ok=True)
    assert d._verify_manifest_workspace(db, spec, task, spec_dir) == ([], [])
