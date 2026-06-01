"""Tests for Database.list_artifacts.

This accessor replaced two raw-SQL blocks that had leaked into the
orchestrator daemon (one even carried a "we don't have a dedicated db method
yet" comment). These lock in the read path the daemon now depends on: full
listing, kind filtering, and oldest-first ordering.

Each test builds a throwaway DB under tmp_path, so the live task store is never
touched.
"""
import pytest

from qwen_autonomous.db import Database
from qwen_autonomous.models import ArtifactKind


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield d
    d.close_all()


@pytest.fixture
def spec(db):
    return db.create_spec(title="t", source_md_path="spec.md")


def test_empty_when_no_artifacts(db, spec):
    assert db.list_artifacts(spec.id) == []


def test_returns_all_kinds_unfiltered(db, spec):
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.DESIGN, path="design.md")
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.CODE, path="a.py")
    arts = db.list_artifacts(spec.id)
    assert {a.kind for a in arts} == {ArtifactKind.DESIGN, ArtifactKind.CODE}
    assert len(arts) == 2


def test_kind_filter_returns_only_that_kind(db, spec):
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.DESIGN, path="design.md")
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.CODE, path="a.py")
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.CODE, path="b.py")
    code = db.list_artifacts(spec.id, kind=ArtifactKind.CODE)
    assert [a.path for a in code] == ["a.py", "b.py"]
    assert all(a.kind is ArtifactKind.CODE for a in code)


def test_ordered_oldest_first(db, spec):
    for name in ("first.py", "second.py", "third.py"):
        db.create_artifact(spec_id=spec.id, kind=ArtifactKind.CODE, path=name)
    paths = [a.path for a in db.list_artifacts(spec.id, kind=ArtifactKind.CODE)]
    assert paths == ["first.py", "second.py", "third.py"]


def test_scoped_to_spec(db):
    s1 = db.create_spec(title="one", source_md_path="1.md")
    s2 = db.create_spec(title="two", source_md_path="2.md")
    db.create_artifact(spec_id=s1.id, kind=ArtifactKind.CODE, path="only-s1.py")
    assert [a.path for a in db.list_artifacts(s2.id)] == []
    assert [a.path for a in db.list_artifacts(s1.id)] == ["only-s1.py"]


def test_row_roundtrips_all_fields(db, spec):
    created = db.create_artifact(
        spec_id=spec.id, kind=ArtifactKind.REVIEW_REPORT,
        path="review_report.md", sha256="abc123",
    )
    [fetched] = db.list_artifacts(spec.id)
    assert fetched.id == created.id
    assert fetched.spec_id == spec.id
    assert fetched.kind is ArtifactKind.REVIEW_REPORT
    assert fetched.path == "review_report.md"
    assert fetched.sha256 == "abc123"
    assert fetched.created_at is not None
