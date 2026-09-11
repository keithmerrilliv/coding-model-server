"""DEV-642: the artifact ledger — one door, three guards, a persistent record."""
from __future__ import annotations

import json

import pytest

from coding_model_autonomous import ArtifactKind
from coding_model_autonomous.db import Database
from coding_model_autonomous.workspace import (
    ACTION_RENAMED, ACTION_RESTORED, ACTION_WRITTEN, LEDGER_FILE,
    REFUSED_COLLISION, REFUSED_EMPTYING, REFUSED_PLACEHOLDER, REFUSED_SHRINK,
    ArtifactLedger,
    CollisionPolicy, attempt_files_from_ledger, read_entries, renamed_path,
)

CODE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
TESTS = "def test_a():\n    assert True\n"
BIG = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(60))  # 180 lines, 60 decls
STUB = "def f0():\n    return 0\n"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def ledger(db):
    spec = db.create_spec(title="t", source_md_path="spec.md")
    return ArtifactLedger.open(db, spec)


class TestPlainWrites:
    def test_write_lands_with_row_and_entry(self, ledger, db):
        out = ledger.write("src/a.py", CODE, role="implementer", task_id=None, retry=0)
        assert out.ok and out.action == ACTION_WRITTEN and out.path == "src/a.py"
        assert (ledger.spec_dir / "src/a.py").read_text() == CODE
        rows = db.list_artifacts(ledger.spec_id, kind=ArtifactKind.CODE)
        assert [(r.path, r.role) for r in rows] == [("src/a.py", "implementer")]
        assert rows[0].sha256 == out.sha256 and len(out.sha256) == 64
        entries = read_entries(ledger.spec_dir)
        assert entries[0].role == "implementer" and entries[0].decls == 2

    def test_same_role_overwrites_itself(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer")
        out = ledger.write("src/a.py", CODE + "\n\ndef c():\n    pass\n", role="implementer")
        assert out.action == ACTION_WRITTEN
        assert ledger.producer("src/a.py").decls == 3

    def test_traversal_is_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.write("../escape.py", CODE, role="implementer")

    def test_note_is_unrecorded(self, ledger, db):
        assert ledger.note("test_output.txt", "4 passed") is not None
        assert read_entries(ledger.spec_dir) is None  # no ledger file was created
        assert db.list_artifacts(ledger.spec_id) == []

    def test_ledger_survives_reopen(self, ledger, db):
        ledger.write("src/a.py", CODE, role="implementer")
        again = ArtifactLedger(db, ledger.spec_id, ledger.spec_dir)
        assert again.producer("src/a.py").role == "implementer"


class TestCollision:
    def test_rename_keeps_both(self, ledger):
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        out = ledger.write("tests/test_x.py", TESTS + "# reviewer\n", role="reviewer",
                           kind=ArtifactKind.TEST_REPORT)
        assert out.action == ACTION_RENAMED
        assert out.path == "tests/test_reviewer_x.py" and out.prior_role == "implementer"
        assert (ledger.spec_dir / "tests/test_x.py").read_text() == TESTS
        assert "# reviewer" in (ledger.spec_dir / "tests/test_reviewer_x.py").read_text()
        assert "collision policy: rename" in out.describe()

    def test_refuse_policy_drops_the_write(self, db):
        spec = db.create_spec(title="t", source_md_path="spec.md")
        ledger = ArtifactLedger.open(db, spec, policy=CollisionPolicy.REFUSE)
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        out = ledger.write("tests/test_x.py", "# stub\n", role="reviewer")
        assert out.action == REFUSED_COLLISION and not out.ok
        assert (ledger.spec_dir / "tests/test_x.py").read_text() == TESTS
        assert not (ledger.spec_dir / "tests/test_reviewer_x.py").exists()

    def test_no_producer_when_file_was_wiped(self, ledger):
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        (ledger.spec_dir / "tests/test_x.py").unlink()
        out = ledger.write("tests/test_x.py", TESTS, role="reviewer")
        assert out.action == ACTION_WRITTEN

    def test_no_producer_when_bytes_changed_behind_the_ledger(self, ledger):
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        (ledger.spec_dir / "tests/test_x.py").write_text(TESTS + "# edited by hand\n")
        assert ledger.producer("tests/test_x.py") is None

    def test_synthesis_may_supersede_the_implementer(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer")
        out = ledger.write("src/a.py", CODE + "\n# merged\n", role="synthesizer")
        assert out.action == ACTION_WRITTEN

    def test_reviewer_rename_that_also_collides_is_refused(self, ledger):
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        ledger.write("tests/test_reviewer_x.py", TESTS, role="implementer")
        out = ledger.write("tests/test_x.py", TESTS, role="reviewer")
        assert out.action == REFUSED_COLLISION and "rename target" in out.detail

    def test_swift_rename(self):
        assert renamed_path("ElectricSheepTests/ElectricSheepTests.swift", "reviewer") == \
            "ElectricSheepTests/reviewer_ElectricSheepTests.swift"


class TestEmptying:
    def test_declarations_to_none_is_refused(self, ledger):
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        out = ledger.write("tests/test_x.py", "// Tests already provided.\n", role="implementer")
        assert out.action == REFUSED_EMPTYING
        assert (ledger.spec_dir / "tests/test_x.py").read_text() == TESTS

    def test_prose_over_prose_is_fine(self, ledger):
        ledger.write("README.md", "# one\n", role="implementer")
        assert ledger.write("README.md", "# two\n", role="implementer").ok


class TestGuardsAreScopedToCode:
    """DEV-647: guards 2 and 3 read the content as CODE — both count
    declarations — so a document's score is an accident of how many
    signatures its author happened to quote. Run 26's 81-line design REVISION
    scored 0 against the 61-line original's 1 and was refused; the
    design-review loop spent a dispatch, changed nothing, and the gate opened
    over the document the reviewer had just failed."""

    DESIGN_WITH_CODE = ("# Design\n\n## API\n\n```python\n"
                        "def is_placeholder_path(rel_path: str) -> bool: ...\n```\n")
    DESIGN_PROSE_ONLY = ("# Design (revised)\n\n## Overview\n\n"
                         + "Prose describing the change in detail.\n" * 40)

    def test_a_prose_design_revision_lands_over_one_that_quoted_code(self, ledger):
        """The exact run-26 sequence."""
        first = ledger.write("design.md", self.DESIGN_WITH_CODE,
                             role="architect", kind=ArtifactKind.DESIGN)
        assert first.ok
        revision = ledger.write("design.md", self.DESIGN_PROSE_ONLY,
                                role="architect", kind=ArtifactKind.DESIGN)
        assert revision.ok, revision.describe()
        assert (ledger.spec_dir / "design.md").read_text() == self.DESIGN_PROSE_ONLY

    def test_the_same_content_pair_is_still_refused_as_code(self, ledger):
        """The guard is not weakened — only scoped. Identical bytes at a CODE
        artifact still trip it."""
        ledger.write("src/a.py", self.DESIGN_WITH_CODE, role="implementer")
        out = ledger.write("src/a.py", self.DESIGN_PROSE_ONLY, role="implementer")
        assert out.action == REFUSED_EMPTYING

    def test_shrink_does_not_judge_a_design_either(self, ledger):
        ledger.record_baseline([("design.md", BIG)])
        out = ledger.write("design.md", "# Design\n\nOne short paragraph.\n",
                           role="architect", kind=ArtifactKind.DESIGN)
        assert out.ok

    def test_a_report_is_not_judged_as_code(self, ledger):
        """The diagnostic kinds are documents too."""
        ledger.write("test_report.md", self.DESIGN_WITH_CODE,
                     role="reviewer", kind=ArtifactKind.TEST_REPORT)
        out = ledger.write("test_report.md", "# Report\n\nAll green.\n",
                           role="reviewer", kind=ArtifactKind.TEST_REPORT)
        assert out.ok

    def test_the_path_guards_still_apply_to_every_kind(self, ledger):
        """Scoping the CONTENT guards must not disarm the path guards: a
        placeholder path is not a design document either (DEV-646)."""
        out = ledger.write("path", "# Design\n", role="architect",
                           kind=ArtifactKind.DESIGN)
        assert out.action == REFUSED_PLACEHOLDER


class TestShrink:
    def test_stub_over_repo_baseline_is_refused(self, ledger):
        ledger.record_baseline([("src/big.py", BIG)])
        out = ledger.write("src/big.py", STUB, role="synthesizer")
        assert out.action == REFUSED_SHRINK
        assert "against 180 / 60" in out.detail
        assert not (ledger.spec_dir / "src/big.py").exists()

    def test_real_edit_passes(self, ledger):
        ledger.record_baseline([("src/big.py", BIG)])
        edited = BIG.replace("def f3():", "def f3(x=1):")
        assert ledger.write("src/big.py", edited, role="implementer").ok

    def test_small_baselines_are_not_checked(self, ledger):
        ledger.record_baseline([("src/small.py", CODE)])
        assert ledger.write("src/small.py", "x = 1\n", role="implementer").ok

    def test_baseline_persists_across_instances(self, ledger, db):
        ledger.record_baseline([("src/big.py", BIG)])
        again = ArtifactLedger(db, ledger.spec_id, ledger.spec_dir)
        assert again.baseline("src/big.py").lines == 180
        assert again.write("src/big.py", STUB, role="synthesizer").action == REFUSED_SHRINK

    def test_size_block_flags_the_shrink(self, ledger):
        ledger.record_baseline([("src/big.py", BIG), ("src/new.py", CODE)])
        (ledger.spec_dir / "src").mkdir(parents=True)
        (ledger.spec_dir / "src/big.py").write_text(STUB)
        (ledger.spec_dir / "src/new.py").write_text(CODE)
        block = ledger.size_block(["src/big.py", "src/new.py", "src/big.py"], "Sizes")
        assert "2 line(s) vs 180" in block and "far smaller" in block
        assert "6 line(s) vs 6" in block
        assert block.count("`src/big.py`") == 1  # rows repeat across retries; the table must not


class TestRestoreAndReads:
    def test_restore_skips_guards_but_records(self, ledger):
        ledger.record_baseline([("src/big.py", BIG)])
        out = ledger.restore("src/big.py", STUB, role="synthesis_repair")
        assert out.action == ACTION_RESTORED and out.ok
        assert ledger.producer("src/big.py").action == ACTION_RESTORED

    def test_hashes_are_disk_bytes(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer")
        (ledger.spec_dir / "src/a.py").write_text("changed\n")
        h = ledger.hashes(["src/a.py", "missing.py"])
        assert set(h) == {"src/a.py"}
        assert h["src/a.py"] != ledger.producer("src/a.py")  # producer is None now
        assert ledger.producer("src/a.py") is None

    def test_landed_paths_by_role(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer")
        ledger.write("tests/test_r.py", TESTS, role="reviewer", kind=ArtifactKind.TEST_REPORT)
        assert ledger.landed_paths(roles=["implementer"]) == ["src/a.py"]
        assert ledger.landed_paths() == ["src/a.py", "tests/test_r.py"]

    def test_outcomes_block_lists_renames_and_refusals(self, ledger):
        ledger.write("tests/test_x.py", TESTS, role="implementer")
        ledger.write("tests/test_x.py", TESTS, role="reviewer")
        ledger.write("tests/test_x.py", "# nothing\n", role="implementer")
        block = ArtifactLedger.outcomes_block(ledger.outcomes, "Ledger")
        assert "test_reviewer_x.py" in block and "REFUSED" in block
        assert ArtifactLedger.outcomes_block(ledger.outcomes[:1], "Ledger") == ""

    def test_corrupt_ledger_starts_empty(self, ledger, db, caplog):
        (ledger.spec_dir / LEDGER_FILE).write_text("{not json")
        again = ArtifactLedger(db, ledger.spec_id, ledger.spec_dir)
        assert again.entries == [] and again.baselines == {}
        assert "unreadable" in caplog.text


class TestCorpusSelection:
    def test_attempt_files_come_from_the_ledger(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer", retry=0)
        ledger.write("tests/test_r.py", TESTS, role="reviewer",
                     kind=ArtifactKind.TEST_REPORT, retry=0)
        (ledger.spec_dir / ".repo_overlay/src").mkdir(parents=True)
        (ledger.spec_dir / ".repo_overlay/src/x.py").write_text("x = 1\n")
        entries = read_entries(ledger.spec_dir)
        files = attempt_files_from_ledger(ledger.spec_dir, entries, 0)
        assert files == {"src/a.py": CODE}

    def test_retry_filter_falls_back_when_no_match(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer", retry=2)
        entries = read_entries(ledger.spec_dir)
        assert attempt_files_from_ledger(ledger.spec_dir, entries, 0) == {"src/a.py": CODE}
        assert attempt_files_from_ledger(ledger.spec_dir, [], 0) is None

    def test_entries_are_json(self, ledger):
        ledger.write("src/a.py", CODE, role="implementer")
        data = json.loads((ledger.spec_dir / LEDGER_FILE).read_text())
        assert set(data) == {"entries", "baselines"}
        assert data["entries"][0]["design_digest"] == ""
