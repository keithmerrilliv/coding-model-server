"""Synthesis must know which attempts predate the current design — DEV-553.

Run 10 of DEV-102 produced four designs: the architect was sent back twice by a
human and once by DEV-468's upstream routing, and each revision changed the
API. Synthesis then merged six attempts spanning all four, and its build failed
on a signature a reviewer had explicitly struck two revisions earlier:

    binary operator '==' cannot be applied to two
    '[(position: Position, damage: Int)]' operands

The merge instruction — take the union of behaviours that worked — is sound
only when every attempt targeted the same contract. Once the architect revises,
some attempts implement an API that has been rejected, and the stale variant
can simply be more numerous than the current one.

The evidence needed to tell them apart was already on disk: _snapshot_retry
copies the whole spec_dir, so every retry_history/retry_N/ carries the
design.md that was current when it ran.
"""
import hashlib

import pytest

from coding_model_autonomous import executor
from coding_model_autonomous.retry_policy import _read_retry_attempts, _snapshot_retry

DESIGN_A = "# design v1\n\nfunc snapshotMushrooms() -> [(position: Position, damage: Int)]\n"
DESIGN_B = "# design v2\n\nfunc snapshotMushrooms() -> [Position: Mushroom]\n"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


# ── the attempts carry their design ──────────────────────────────────────────

def test_each_snapshot_records_the_design_it_ran_against(tmp_path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()

    (spec_dir / "design.md").write_text(DESIGN_A)
    (spec_dir / "World.swift").write_text("struct World { /* v1 */ }\n")
    _snapshot_retry(spec_dir, retry_index=0)

    # the architect revises, and a later attempt runs against the new design
    (spec_dir / "design.md").write_text(DESIGN_B)
    (spec_dir / "World.swift").write_text("struct World { /* v2 */ }\n")
    _snapshot_retry(spec_dir, retry_index=1)

    attempts = _read_retry_attempts(spec_dir)
    by_retry = {a["retry"]: a for a in attempts}
    assert by_retry[0]["design_digest"] == _digest(DESIGN_A)
    assert by_retry[1]["design_digest"] == _digest(DESIGN_B)
    assert by_retry[0]["design_digest"] != by_retry[1]["design_digest"]


def test_a_missing_design_leaves_the_digest_empty_not_wrong(tmp_path):
    """An empty digest means 'unknown', which must never be read as stale."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "World.swift").write_text("struct World {}\n")
    _snapshot_retry(spec_dir, retry_index=0)

    attempts = _read_retry_attempts(spec_dir)
    assert attempts[0]["design_digest"] == ""

    text = executor.build_synthesis_message(
        "# spec", DESIGN_B, attempts,
        current_design_digest=_digest(DESIGN_B))[-1]["content"]
    assert "SUPERSEDED" not in text


# ── the prompt says so ───────────────────────────────────────────────────────

def _attempt(retry, digest, body="struct A {}"):
    return {"retry": retry, "agent": "snapshot", "test_summary": "",
            "files": {"A.swift": body}, "design_digest": digest}


def test_superseded_attempts_are_marked_and_explained():
    attempts = [_attempt(0, _digest(DESIGN_A)), _attempt(1, _digest(DESIGN_B))]
    text = executor.build_synthesis_message(
        "# spec", DESIGN_B, attempts,
        current_design_digest=_digest(DESIGN_B))[-1]["content"]

    assert "retry=0 (agent=snapshot) — SUPERSEDED design" in text
    assert "retry=1 (agent=snapshot) — current design" in text
    # and the instruction that resolves the conflict
    assert "1 of these 2 attempts" in text
    assert "the design above is correct and the attempt is wrong" in text


def test_no_marking_when_every_attempt_matches():
    attempts = [_attempt(0, _digest(DESIGN_B)), _attempt(1, _digest(DESIGN_B))]
    text = executor.build_synthesis_message(
        "# spec", DESIGN_B, attempts,
        current_design_digest=_digest(DESIGN_B))[-1]["content"]
    assert "SUPERSEDED" not in text
    assert "current design" in text


@pytest.mark.parametrize("digest", [None, ""])
def test_without_a_current_digest_the_prompt_is_unchanged(digest):
    """Older specs whose snapshots predate this change must see no difference."""
    attempts = [_attempt(0, _digest(DESIGN_A))]
    with_arg = executor.build_synthesis_message(
        "# spec", DESIGN_B, attempts, current_design_digest=digest)
    without = executor.build_synthesis_message("# spec", DESIGN_B, attempts)
    assert with_arg == without
    assert "SUPERSEDED" not in with_arg[-1]["content"]


def test_the_attempts_themselves_are_never_dropped():
    """Marking, not filtering. Run 10 had exactly ONE attempt against the
    final design, so filtering would have left nothing to merge."""
    attempts = [_attempt(0, _digest(DESIGN_A), "struct Old {}"),
                _attempt(1, _digest(DESIGN_A), "struct Older {}"),
                _attempt(2, _digest(DESIGN_B), "struct New {}")]
    text = executor.build_synthesis_message(
        "# spec", DESIGN_B, attempts,
        current_design_digest=_digest(DESIGN_B))[-1]["content"]
    for body in ("struct Old {}", "struct Older {}", "struct New {}"):
        assert body in text, "a superseded attempt's behaviour is still useful"
