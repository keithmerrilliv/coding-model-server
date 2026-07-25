"""JiraSync forward/reverse behavior against FakeJiraClient (DEV-129).

The sync worker is the bridge between the SQLite source of truth and the
human acting in Jira; it previously had zero tests. These drive tick()
directly (no thread) and pin: forward mirroring of specs and gates,
watermark idempotency, and reverse application of human decisions.
"""
import pytest

from coding_model_autonomous.db import Database
from coding_model_autonomous.jira_client import (
    FakeJiraClient,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_REJECTED,
)
from coding_model_autonomous.jira_sync import JiraSync
from coding_model_autonomous.models import GateStatus, GateType, SpecStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def sync(db):
    return JiraSync(db, FakeJiraClient())


@pytest.fixture
def spec_and_gate(db):
    spec = db.create_spec(title="demo spec", source_md_path="spec.md")
    (db.spec_dir(spec.id) / "spec.md").write_text("# demo spec\ndo the thing")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    gate = db.create_gate(spec_id=spec.id, gate_type=GateType.RELEASE_APPROVAL,
                          prompt_md="## approve release?")
    return spec, gate


def test_forward_mirrors_spec_and_gate(db, sync, spec_and_gate):
    spec, gate = spec_and_gate
    sync.tick()

    spec = db.get_spec(spec.id)
    gate = db.get_gate(gate.id)
    assert spec.jira_epic_key is not None
    assert gate.jira_issue_key is not None
    issue = sync.client.get_issue(gate.jira_issue_key)
    assert issue.parent_key == spec.jira_epic_key
    # Gate issues land in "In Review" so the human knows it's on them.
    assert issue.status == STATUS_IN_REVIEW


def test_forward_is_idempotent_across_ticks(db, sync, spec_and_gate):
    sync.tick()
    creates = [c for c in sync.client.call_log
               if c[0] in ("create_epic", "create_issue")]
    sync.tick()
    creates_after = [c for c in sync.client.call_log
                     if c[0] in ("create_epic", "create_issue")]
    assert creates_after == creates  # watermark advanced; nothing re-mirrored


def test_reverse_applies_human_approval_with_comment_as_notes(
        db, sync, spec_and_gate):
    spec, gate = spec_and_gate
    sync.tick()
    issue_key = db.get_gate(gate.id).jira_issue_key

    # Human approves in the Jira UI: comment + move to Done.
    sync.client.add_comment(issue_key, "ship it")
    sync.client.transition_issue(issue_key, STATUS_DONE)
    sync.tick()

    gate = db.get_gate(gate.id)
    assert gate.status is GateStatus.APPROVED
    assert gate.reviewer_notes == "ship it"


def test_reverse_applies_human_rejection(db, sync, spec_and_gate):
    spec, gate = spec_and_gate
    sync.tick()
    issue_key = db.get_gate(gate.id).jira_issue_key

    sync.client.transition_issue(issue_key, STATUS_REJECTED)
    sync.tick()

    assert db.get_gate(gate.id).status is GateStatus.REJECTED


def test_reverse_leaves_untouched_issue_pending(db, sync, spec_and_gate):
    spec, gate = spec_and_gate
    sync.tick()
    sync.tick()  # human hasn't acted; nothing may change
    assert db.get_gate(gate.id).status is GateStatus.PENDING


# ── DEV-149: rejecting from a stock workflow (no "Rejected" status) ──────────

def test_done_with_reject_comment_is_a_rejection(db, sync, spec_and_gate):
    # Stock Jira Cloud workflows only reach Done — which used to read back
    # as approval no matter what the human meant. The REJECT comment token
    # flips it, and the comment (reasons included) becomes the notes.
    spec, gate = spec_and_gate
    sync.tick()
    issue_key = db.get_gate(gate.id).jira_issue_key

    sync.client.add_comment(issue_key, "REJECT — wrong approach, use the v2 API")
    sync.client.transition_issue(issue_key, STATUS_DONE)
    sync.tick()

    gate = db.get_gate(gate.id)
    assert gate.status is GateStatus.REJECTED
    assert "v2 API" in gate.reviewer_notes


def test_rejected_casing_variant_also_counts(db, sync, spec_and_gate):
    spec, gate = spec_and_gate
    sync.tick()
    issue_key = db.get_gate(gate.id).jira_issue_key

    sync.client.add_comment(issue_key, "rejected: tests are missing")
    sync.client.transition_issue(issue_key, STATUS_DONE)
    sync.tick()

    assert db.get_gate(gate.id).status is GateStatus.REJECTED


def test_plain_done_with_normal_comment_still_approves(db, sync, spec_and_gate):
    # A comment that merely CONTAINS the word reject mid-sentence must not
    # flip an approval — only a comment that STARTS with the token does.
    spec, gate = spec_and_gate
    sync.tick()
    issue_key = db.get_gate(gate.id).jira_issue_key

    sync.client.add_comment(issue_key, "I almost decided to reject, but ship it")
    sync.client.transition_issue(issue_key, STATUS_DONE)
    sync.tick()

    assert db.get_gate(gate.id).status is GateStatus.APPROVED
