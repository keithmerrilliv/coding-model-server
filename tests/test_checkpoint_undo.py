"""DEV-132/133/134 — checkpoint, /undo, and write-loop correctness.

DEV-132: creating a new file pushed no checkpoint, so /undo popped an older
entry and restored an unrelated file. Creation now pushes a sentinel that
/undo resolves by deleting the created file.

DEV-133: checkpoint names had 1-second granularity; two edits to one file
within a second overwrote the first backup (losing the true original) and
left two stack entries pointing at one file.

DEV-134: the per-file write cap counted only WRITE_FILE; EDIT_FILE could
thrash a file unboundedly.
"""
import pytest

from coding_model_server import tool_handlers as th
from coding_model_server.tool_state import state


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    class _Cfg:
        CHUNK_THRESHOLD = 1_000_000
        CHUNK_SIZE = 100_000
        CHUNK_OVERLAP = 200
        ALLOW_SHELL_MODE = True

    colors = {k: "" for k in (
        "BLUE", "GREEN", "RED", "FAIL", "WARNING", "CYAN", "YELLOW",
        "BOLD", "ENDC", "DIM", "MAGENTA",
    )}
    th.configure(
        config=_Cfg(),
        colors=colors,
        print_colored_fn=lambda *a, **k: None,
        logger_inst=__import__("logging").getLogger("test"),
        temp_tracker={"add": lambda p: None},
        external_handlers={},
        permission_mode="yolo",
        verbose_mode=False,
    )
    th.reset_write_counts()
    state.checkpoint_stack.clear()
    monkeypatch.setattr(state, "CHECKPOINT_DIR", str(tmp_path / ".checkpoints"))
    ok, _ = th.set_workspace(str(tmp_path))
    assert ok
    yield
    state.checkpoint_stack.clear()
    state.workspace_root = None


class TestUndoCreation:
    def test_undo_of_a_new_file_deletes_it(self, tmp_path):
        # DEV-132: the exact scenario — edit an existing file, then create a
        # new one. /undo must remove the NEW file, not touch the older one.
        existing = tmp_path / "old.txt"
        existing.write_text("v1")
        th.process_remote_commands(f"<<<WRITE_FILE>>>{existing}\nv2")
        assert existing.read_text().strip() == "v2"

        th.process_remote_commands(f"<<<WRITE_FILE>>>{tmp_path}/new.py\nprint('hi')")
        assert (tmp_path / "new.py").exists()

        msg = th.undo_last_checkpoint()
        assert "new.py" in msg and "Removed" in msg
        assert not (tmp_path / "new.py").exists(), "undo of a creation deletes it"
        assert existing.read_text().strip() == "v2", "older file untouched"

        # A second undo now legitimately reverts the earlier edit.
        msg2 = th.undo_last_checkpoint()
        assert "old.txt" in msg2
        assert existing.read_text() == "v1"


class TestCheckpointCollisions:
    def test_two_same_second_edits_keep_distinct_backups(self, tmp_path):
        # DEV-133: successive writes land microseconds apart. Both backups
        # must survive so two undos walk back to the true original.
        f = tmp_path / "hot.txt"
        f.write_text("original")
        th.process_remote_commands(f"<<<WRITE_FILE>>>{f}\nsecond")
        th.process_remote_commands(f"<<<WRITE_FILE>>>{f}\nthird")

        backups = [cp for (orig, cp, _) in state.checkpoint_stack if cp]
        assert len(backups) == len(set(backups)) == 2, "distinct backup files"

        th.undo_last_checkpoint()
        assert f.read_text().strip() == "second"
        th.undo_last_checkpoint()
        assert f.read_text() == "original", "the true original must survive"


class TestEditWriteLoop:
    def test_edit_file_counts_against_the_write_cap(self, tmp_path):
        # DEV-134: EDIT_FILE was exempt from the documented per-file cap.
        f = tmp_path / "loop.txt"
        f.write_text("value = 0\n")
        for i in range(state.MAX_WRITES_PER_FILE):
            out = th.edit_file_content(
                f"{f}\n<<<OLD>>>\nvalue = {i}\n<<<NEW>>>\nvalue = {i + 1}\n")
            assert out is not None and "Successfully edited" in out

        blocked = th.edit_file_content(
            f"{f}\n<<<OLD>>>\nvalue = {state.MAX_WRITES_PER_FILE}\n"
            f"<<<NEW>>>\nvalue = 999\n")
        assert blocked is None, "over-cap edit must be silently refused"
        assert f.read_text() == f"value = {state.MAX_WRITES_PER_FILE}\n"

    def test_writes_and_edits_share_one_counter(self, tmp_path):
        f = tmp_path / "mix.txt"
        f.write_text("a\n")
        th.process_remote_commands(f"<<<WRITE_FILE>>>{f}\nb")
        th.process_remote_commands(f"<<<WRITE_FILE>>>{f}\nc")
        out = th.edit_file_content(f"{f}\n<<<OLD>>>\nc\n<<<NEW>>>\nd\n")
        assert out is not None, "third mutation still under the cap"
        blocked = th.edit_file_content(f"{f}\n<<<OLD>>>\nd\n<<<NEW>>>\ne\n")
        assert blocked is None, "fourth mutation exceeds the shared cap"
