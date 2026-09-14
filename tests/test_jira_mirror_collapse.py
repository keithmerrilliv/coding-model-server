"""DEV-482: a FAILED spec must not mirror as Done/Done.

A stock Jira Cloud workflow has To Do / In Progress / Done and nothing else,
so "Cancelled" (a FAILED or cancelled spec) and "Rejected" (a rejected gate)
collapse onto Done. The collapse stays — a run has to land somewhere — but
it now carries resolution "Won't Do", a comment saying what happened, and a
warning in the log. A workflow that HAS the status is untouched.
"""
import logging

import pytest

from coding_model_autonomous.db import Database
from coding_model_autonomous.jira_client import (
    LABEL_PIPELINE_FAILED, RESOLUTION_WONT_DO, STATUS_CANCELLED, STATUS_DONE,
    STATUS_IN_PROGRESS, STATUS_REJECTED, STATUS_TODO, FakeJiraClient,
    collapse_note,
)
from coding_model_autonomous.jira_sync import JiraSync
from coding_model_autonomous.models import SpecStatus

STOCK = [STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE]


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


class TestTheClientCollapse:
    def test_cancelled_lands_on_done_with_wont_do_and_a_note(self, caplog):
        client = FakeJiraClient(statuses=STOCK)
        key = client.create_epic("run", "desc")
        with caplog.at_level(logging.WARNING):
            landed = client.transition_issue(key, STATUS_CANCELLED)
        issue = client.get_issue(key)
        assert landed == STATUS_DONE and issue.status == STATUS_DONE
        assert issue.resolution == RESOLUTION_WONT_DO
        assert any("FAILED" in c and "Won't Do" in c and "DEV-482" in c for c in issue.comments)
        assert any("DEV-482" in r.getMessage() for r in caplog.records)

    def test_rejected_collapses_the_same_way(self):
        client = FakeJiraClient(statuses=STOCK)
        key = client.create_issue("gate", "d")
        assert client.transition_issue(key, STATUS_REJECTED) == STATUS_DONE
        issue = client.get_issue(key)
        assert issue.resolution == RESOLUTION_WONT_DO
        assert any("REJECTED" in c for c in issue.comments)

    def test_a_real_done_carries_no_resolution_marker(self):
        client = FakeJiraClient(statuses=STOCK)
        key = client.create_epic("run", "desc")
        assert client.transition_issue(key, STATUS_DONE) == STATUS_DONE
        issue = client.get_issue(key)
        assert issue.resolution is None and issue.comments == []
        assert issue.labels == []  # DEV-680: no label on a real Done either

    def test_a_workflow_with_cancelled_is_untouched(self):
        client = FakeJiraClient()  # every logical status exists
        key = client.create_epic("run", "desc")
        assert client.transition_issue(key, STATUS_CANCELLED) == STATUS_CANCELLED
        issue = client.get_issue(key)
        assert issue.status == STATUS_CANCELLED
        assert issue.resolution is None and issue.comments == []

    def test_failed_and_done_are_distinguishable_by_field(self):
        """The acceptance in one assertion: two epics, both Done, told apart
        by resolution alone."""
        client = FakeJiraClient(statuses=STOCK)
        ok, bad = client.create_epic("ok", ""), client.create_epic("bad", "")
        client.transition_issue(ok, STATUS_DONE)
        client.transition_issue(bad, STATUS_CANCELLED)
        assert client.get_issue(ok).status == client.get_issue(bad).status == STATUS_DONE
        assert client.get_issue(ok).resolution != client.get_issue(bad).resolution


class TestTheNoteTellsTheTruth:
    """DEV-680: AUTO-975 landed Done with resolution Done while the note
    claimed Won't Do — the stock Done transition has no resolution screen.
    The note must describe what landed, and a label JQL can see goes on
    either way."""

    def test_a_refused_resolution_is_not_claimed(self, caplog):
        client = FakeJiraClient(statuses=STOCK, resolution_writable=False)
        key = client.create_epic("run", "desc")
        with caplog.at_level(logging.WARNING):
            landed = client.transition_issue(key, STATUS_CANCELLED)
        issue = client.get_issue(key)
        assert landed == STATUS_DONE and issue.resolution is None
        note = next(c for c in issue.comments if "Mirror note" in c)
        assert "did not accept resolution 'Won't Do'" in note
        assert "with resolution 'Won't Do'" not in note
        assert LABEL_PIPELINE_FAILED in note
        assert issue.labels == [LABEL_PIPELINE_FAILED]
        assert any("resolution UNSET" in r.getMessage() for r in caplog.records)

    def test_a_landed_resolution_is_claimed_with_the_label(self):
        client = FakeJiraClient(statuses=STOCK, resolution_writable=True)
        key = client.create_epic("run", "desc")
        client.transition_issue(key, STATUS_CANCELLED)
        issue = client.get_issue(key)
        assert issue.resolution == RESOLUTION_WONT_DO
        note = next(c for c in issue.comments if "Mirror note" in c)
        assert "with resolution 'Won't Do'" in note
        assert "did not accept" not in note
        assert LABEL_PIPELINE_FAILED in note
        assert issue.labels == [LABEL_PIPELINE_FAILED]

    def test_the_note_wording_matches_what_landed(self):
        claimed = collapse_note(STATUS_CANCELLED, STATUS_DONE, resolution_set=True)
        refused = collapse_note(STATUS_CANCELLED, STATUS_DONE, resolution_set=False)
        assert "with resolution 'Won't Do'" in claimed and "did not accept" not in claimed
        assert "did not accept resolution 'Won't Do'" in refused
        assert "with resolution 'Won't Do'" not in refused
        assert "success" in claimed and "success" in refused  # the warning stays
        assert LABEL_PIPELINE_FAILED not in refused  # only named when it landed
        assert LABEL_PIPELINE_FAILED in collapse_note(
            STATUS_CANCELLED, STATUS_DONE, resolution_set=False, label_set=True)

    def test_the_label_is_not_duplicated_on_a_second_collapse(self):
        client = FakeJiraClient(statuses=STOCK, resolution_writable=False)
        key = client.create_epic("run", "desc")
        client.transition_issue(key, STATUS_CANCELLED)
        client.transition_issue(key, STATUS_CANCELLED)
        assert client.get_issue(key).labels == [LABEL_PIPELINE_FAILED]


class TestThroughTheSync:
    def test_a_failed_spec_mirrors_as_done_wont_do(self, db, caplog):
        client = FakeJiraClient(statuses=STOCK)
        sync = JiraSync(db, client)
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        (db.spec_dir(spec.id) / "spec.md").write_text("# demo")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
        sync.tick()
        key = db.get_spec(spec.id).jira_epic_key
        assert key and client.get_issue(key).status == STATUS_IN_PROGRESS

        db.update_spec_status(spec.id, SpecStatus.FAILED)
        with caplog.at_level(logging.INFO):
            sync.tick()

        issue = client.get_issue(key)
        assert issue.status == STATUS_DONE and issue.resolution == RESOLUTION_WONT_DO
        assert any("Mirror note" in c for c in issue.comments)
        assert any("landed on Done" in r.getMessage() for r in caplog.records)

    def test_a_completed_spec_mirrors_as_done_done(self, db):
        client = FakeJiraClient(statuses=STOCK)
        sync = JiraSync(db, client)
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        (db.spec_dir(spec.id) / "spec.md").write_text("# demo")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
        sync.tick()
        db.update_spec_status(spec.id, SpecStatus.DONE)
        sync.tick()
        issue = client.get_issue(db.get_spec(spec.id).jira_epic_key)
        assert issue.status == STATUS_DONE and issue.resolution is None
