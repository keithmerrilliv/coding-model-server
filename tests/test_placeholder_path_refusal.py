"""Tests for placeholder path refusal guard (DEV-646)."""
import pytest

from coding_model_autonomous.workspace import (
    ArtifactLedger, REFUSALS, REFUSED_PLACEHOLDER,
    is_placeholder_path, read_entries
)


class TestIsPlaceholderPath:
    """T1 & T2: is_placeholder_path returns True/False as specified."""

    def test_t1_placeholder_paths_return_true(self):
        """T1 — Returns True for each of the four paths run 25 landed."""
        assert is_placeholder_path("path") is True
        assert is_placeholder_path("...") is True
        assert is_placeholder_path("another/file.py") is True
        assert is_placeholder_path("relative/path/to/file.ext") is True

    def test_t2_real_paths_return_false(self):
        """T2 — Returns False for seven real paths."""
        real_paths = [
            "src/coding_model_autonomous/workspace.py",
            "tests/test_x.py",
            "README.md",
            "Makefile",
            "LICENSE",
            "src/path/to/thing.py",  # contains 'path/to/' but not as prefix
            "another_module/file_reader.py",
        ]
        for p in real_paths:
            assert is_placeholder_path(p) is False


class TestWriteRefusesPlaceholders:
    """T3, T4, T8: write refuses placeholders without side effects."""

    def test_t3_write_returns_refused_outcome(self, tmp_path):
        """T3 — write('path', ...) yields outcome with REFUSED_PLACEHOLDER."""
        ledger = ArtifactLedger(None, "spec_t", tmp_path)
        outcome = ledger.write("path", "x = 1", role="synthesizer")

        assert outcome.action == REFUSED_PLACEHOLDER
        assert outcome.refused is True
        assert outcome.ok is False
        assert outcome.path is None
        assert (tmp_path / "path").exists() is False

    def test_t4_entry_recorded_in_ledger(self, tmp_path):
        """T4 — After T3, read_entries[-1] has action and requested."""
        ledger = ArtifactLedger(None, "spec_t2", tmp_path)
        ledger.write("path", "x = 1", role="synthesizer")
        entries = read_entries(tmp_path)

        last = entries[-1]
        assert last.action == REFUSED_PLACEHOLDER
        assert last.requested == "path"

    def test_t8_no_directory_created(self, tmp_path):
        """T8 — Placeholder write does not create parent directories."""
        ledger = ArtifactLedger(None, "spec_w", tmp_path)
        outcome = ledger.write("another/file.py", "x = 1", role="synthesizer")

        assert outcome.refused is True
        assert (tmp_path / "another").exists() is False


class TestDescribePlaceholderRefusal:
    """T5: describe() contains backtick-quoted path and word REFUSED."""

    def test_t5_describe_format(self, tmp_path):
        """T5 — Outcome describe() contains `path` and REFUSED."""
        ledger = ArtifactLedger(None, "spec_x", tmp_path)
        outcome = ledger.write("path", "x = 1", role="synthesizer")

        desc = outcome.describe()
        assert "`path`" in desc
        assert "REFUSED" in desc

    def test_refused_placeholder_in_refusals_tuple(self):
        """T5 — REFUSED_PLACEHOLDER in REFUSALS is True."""
        assert REFUSED_PLACEHOLDER in REFUSALS


class TestOrdinaryWritesUnaffected:
    """T6 & T7: ordinary writes work; traversal still raises ValueError."""

    def test_t6_ordinary_write_unaffected(self, tmp_path):
        """T6 — Ordinary write unaffected: writes bytes exactly as given."""
        ledger = ArtifactLedger(None, "spec_u", tmp_path)
        outcome = ledger.write("src/a.py", "x = 1", role="implementer")

        assert outcome.action == "written"
        assert outcome.ok is True
        assert (tmp_path / "src/a.py").read_bytes() == b"x = 1\n"

    def test_t7_traversal_still_raises_valueerror(self, tmp_path):
        """T7 — Traversal '..' still raises ValueError (not soft-refused)."""
        ledger = ArtifactLedger(None, "spec_v", tmp_path)

        with pytest.raises(ValueError, match="Path traversal rejected"):
            ledger.write("..", "x = 1", role="implementer")
