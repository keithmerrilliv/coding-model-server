"""DEV-901: the synthesis replay harness rebuilds what synthesis saw, from the
archive alone, and scores a replay the way the pipeline scored the original.

The fixture is a small archive: two attempts in retry_history, the synthesized
output on top of them, a reviewer note written before synthesis started and
one written after, and a release gate that records the synthesis passed.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind, GateType

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_synthesis.py"
_spec = importlib.util.spec_from_file_location("replay_synthesis", SCRIPT)
rs = importlib.util.module_from_spec(_spec)
sys.modules["replay_synthesis"] = rs  # dataclasses resolve through sys.modules
_spec.loader.exec_module(rs)

PLAN = "test_strategy:\n  framework: pytest\n  required: true\n"
PASS = "============ 1 passed in 0.01s ============"
FAIL = "============ 1 failed in 0.01s ============"
SYNTHESIZED = "def f():\n    return 'synth'\n"


def _set_created(db_path, table, row_id, when):
    conn = sqlite3.connect(db_path)
    conn.execute(f"UPDATE {table} SET created_at=? WHERE id=?", (when, row_id))
    conn.commit()
    conn.close()


def _event(db_path, spec_id, kind, payload, when):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO events (spec_id, kind, payload_json, created_at) "
                 "VALUES (?, ?, ?, ?)", (spec_id, kind, json.dumps(payload), when))
    conn.commit()
    conn.close()


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    db = Database(db_path=root / "tasks.sqlite", workspace_root=root / "specs")
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    early = db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW,
                           prompt_md="## Review")
    db.respond_to_gate(early.id, "rejected", notes="EARLY NOTE: keep attempt 0's f")
    late = db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW,
                          prompt_md="## Review")
    db.respond_to_gate(late.id, "rejected", notes="LATE NOTE: written after")
    db.close_all()
    db_path = root / "tasks.sqlite"
    _set_created(db_path, "tasks", task.id, "2026-08-31T00:00:00+00:00")
    _set_created(db_path, "review_gates", early.id, "2026-09-01T00:00:00+00:00")
    _set_created(db_path, "review_gates", late.id, "2026-09-01T02:00:00+00:00")
    # The call ran for an hour and was recorded at 01:30, so it started at 00:30.
    _event(db_path, spec.id, "agent_ran",
           {"role": "synthesizer", "attempts": 2, "attempts_total": 2,
            "agent": "deep_reviewer", "duration_ms": 3_600_000,
            "prompt_tokens": 100}, "2026-09-01T01:30:00+00:00")
    _event(db_path, spec.id, "agent_ran",
           {"role": "synthesizer", "model_call": False, "normalized": ["x"]},
           "2026-09-01T01:30:01+00:00")
    _event(db_path, spec.id, "gate_created", {"gate_type": "release_approval"},
           "2026-09-01T01:31:00+00:00")

    ws = root / "specs" / spec.id
    ws.mkdir(parents=True, exist_ok=True)
    for name, text in (("spec.md", "# spec"), ("design.md", "# design"),
                       ("plan.yaml", PLAN)):
        (ws / name).write_text(text)
    for i, body in enumerate(("return 'attempt0'", "return 'attempt1'")):
        snap = ws / "retry_history" / f"retry_{i}"
        snap.mkdir(parents=True)
        (snap / "impl.py").write_text(f"def f():\n    {body}\n")
        (snap / "test_output.txt").write_text(FAIL)
        for name in ("spec.md", "design.md", "plan.yaml"):
            (snap / name).write_text((ws / name).read_text())
    (ws / "retry_history" / "synthesis").mkdir()
    (ws / "impl.py").write_text(SYNTHESIZED)
    return root, spec.id


def _only(root):
    [syn] = [s for calls in rs.find_syntheses(root / "tasks.sqlite").values()
             for s in calls]
    return syn


def _replay(root, tmp_path, agent="deep_reviewer", **kw):
    return rs.replay(_only(root), agent, tmp_path / "scratch",
                     archive_db=root / "tasks.sqlite",
                     archive_specs=root / "specs", **kw)


def test_the_archive_is_read_as_one_synthesis_with_its_verdict(archive):
    root, spec_id = archive
    syntheses = rs.find_syntheses(root / "tasks.sqlite")
    # The model_call=False record filed under the role is not a second call.
    assert [s.attempts_total for s in syntheses[spec_id]] == [2]
    syn = syntheses[spec_id][0]
    assert syn.started_at == "2026-09-01T00:30:00+00:00"
    assert syn.archived_verdict == "passed"


def test_the_prompt_is_what_synthesis_saw_and_is_byte_stable(archive, tmp_path):
    root, _ = archive
    real = d.build_synthesis_message
    with mock.patch.object(d, "build_synthesis_message", wraps=real) as built:
        first = _replay(root, tmp_path, prompt_only=True)
        second = _replay(root, tmp_path, "dense_architect", prompt_only=True)
    assert first["status"] == second["status"] == "prompt_only"
    # Every arm is sent the same bytes.
    assert first["calls"][0]["prompt_sha256"] == second["calls"][0]["prompt_sha256"]
    text = json.dumps(built.call_args.args[2]) + json.dumps(built.call_args.kwargs)
    assert "attempt0" in text and "attempt1" in text
    assert "synth" not in text, "the synthesized output leaked into its own prompt"
    assert "EARLY NOTE" in text and "LATE NOTE" not in text
    # The last attempt is the live one, as it was when synthesis ran.
    agents = [a["agent"] for a in built.call_args.args[2]]
    assert agents == ["snapshot", "current"]


def test_the_archive_is_never_written(archive, tmp_path):
    root, spec_id = archive

    def state():
        # SQLite creates the -shm/-wal sidecars on any read of a WAL database;
        # the live one always has them. The database file itself must not move.
        return {p: p.stat().st_mtime_ns for p in root.rglob("*")
                if p.is_file() and not p.name.endswith(("-shm", "-wal"))}

    before = state()
    _replay(root, tmp_path, prompt_only=True)
    assert state() == before


def _score(root, tmp_path, response):
    def fake_tests(spec_dir, framework="pytest", **_):
        ok = (Path(spec_dir) / "impl.py").read_text() == SYNTHESIZED
        return ok, PASS if ok else FAIL

    with mock.patch.object(d, "call_agent", return_value=response), \
            mock.patch.object(d, "run_tests", side_effect=fake_tests):
        return _replay(root, tmp_path)


def test_a_replay_of_the_archived_output_scores_as_the_archive_did(archive, tmp_path):
    root, _ = archive
    row = _score(root, tmp_path,
                 f"<<<FILE: impl.py>>>\n{SYNTHESIZED}<<<END_FILE>>>")
    assert row["status"] == "scored"
    assert row["final"]["passed"] is True
    assert row["final"]["passed"] == (row["archived"]["verdict"] == "passed")
    assert (row["final"]["passed_n"], row["final"]["total"]) == (1, 1)


def test_a_worse_replay_scores_as_a_failure(archive, tmp_path):
    root, _ = archive
    row = _score(root, tmp_path,
                 "<<<FILE: impl.py>>>\ndef f():\n    return 'attempt1'\n<<<END_FILE>>>")
    assert row["status"] == "scored"
    assert row["final"]["passed"] is False
    assert row["final"]["verdict"] == "tests_failed"


def test_an_unparseable_replay_is_not_a_test_failure(archive, tmp_path):
    root, _ = archive
    row = _score(root, tmp_path, "no file blocks here")
    assert row["status"] in ("parse_failed", "no_verdict")
    assert "final" not in row


def test_a_missing_attempt_is_unreproducible_not_failed(archive, tmp_path):
    root, spec_id = archive
    import shutil
    shutil.rmtree(root / "specs" / spec_id / "retry_history" / "retry_0")
    row = _replay(root, tmp_path, prompt_only=True)
    assert row["status"] == "unreproducible"
    assert "retry_0" in row["reason"]


def _commit_at(root, when, message):
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", message], check=True, capture_output=True, env=env)


def test_a_self_target_overlay_is_the_pinned_commit_both_halves(archive, tmp_path,
                                                               monkeypatch):
    """The overlay's src/ AND the tree the existing tests come from are read at
    the commit synthesis ran against (main as of 2026-09-01 here), never at
    HEAD. The repository is a throwaway one with backdated commits: CI checks
    out one commit deep, so the real history cannot stand in for it."""
    root, spec_id = archive
    repo = tmp_path / rs.REPO.name
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "src" / "pkg" / "old.py").write_text("OLD\n")
    (repo / "tests" / "test_old.py").write_text("def test_old(): pass\n")
    _commit_at(repo, "2026-08-31T00:00:00+00:00", "before synthesis")
    (repo / "src" / "pkg" / "settings.py").write_text("NEW\n")
    (repo / "tests" / "test_new.py").write_text("def test_new(): pass\n")
    _commit_at(repo, "2026-09-02T00:00:00+00:00", "after synthesis")
    monkeypatch.setattr(rs, "REPO", repo)
    # The daemon reads the plan from the spec row, not from plan.yaml.
    conn = sqlite3.connect(root / "tasks.sqlite")
    conn.execute("UPDATE specs SET normalized_yaml=? WHERE id=?",
                 (PLAN + f"  repo: {repo.name}\n", spec_id))
    conn.commit()
    conn.close()
    seen = {}

    def fake_synthesis(db, spec, task, spec_dir, framework, opts):
        from coding_model_autonomous import test_runner
        out = tmp_path / "o"
        test_runner._extract_committed_src(repo, out)
        test_runner._extract_committed_tree(repo, out)
        seen["old_src"] = (out / "src/pkg/old.py").exists()
        seen["new_src"] = (out / "src/pkg/settings.py").exists()
        seen["old_test"] = (out / "tests/test_old.py").exists()
        seen["new_test"] = (out / "tests/test_new.py").exists()
        return False, ""

    with mock.patch.object(d, "_run_synthesis", fake_synthesis):
        row = _replay(root, tmp_path)
    assert row["pinned_ref"]
    assert seen == {"old_src": True, "new_src": False, "old_test": True, "new_test": False}


def test_the_scratch_database_holds_only_what_preceded_synthesis(archive, tmp_path):
    root, spec_id = archive
    other = Database(db_path=root / "tasks.sqlite", workspace_root=root / "specs")
    other_spec = other.create_spec(title="other", source_md_path="spec.md")
    other.record_event(EventKind.SPEC_SUBMITTED, spec_id=other_spec.id)
    other.close_all()
    dest = rs.build_scratch_db(spec_id, "2026-09-01T00:30:00+00:00",
                               tmp_path / "copy.sqlite", source=root / "tasks.sqlite")
    conn = sqlite3.connect(dest)
    notes = [r[0] for r in conn.execute("SELECT reviewer_notes FROM review_gates")]
    specs = [r[0] for r in conn.execute("SELECT id FROM specs")]
    conn.close()
    assert specs == [spec_id]
    assert notes == ["EARLY NOTE: keep attempt 0's f"]
