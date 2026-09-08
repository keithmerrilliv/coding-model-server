import hashlib
from pathlib import Path

from coding_model_autonomous.workspace import ArtifactLedger, read_entries


def test_write_adds_trailing_newline(tmp_path: Path):
    """T1: write('src/a.py', 'x = 1', role='implementer') produces b'x = 1\\n'; outcome sha256 matches on-disk bytes"""
    ledger = ArtifactLedger(None, "spec_t", tmp_path)
    outcome = ledger.write("src/a.py", "x = 1", role="implementer")

    file_content = (tmp_path / "src" / "a.py").read_bytes()
    expected_content = b"x = 1\n"
    assert file_content == expected_content

    disk_sha256 = hashlib.sha256(file_content).hexdigest()
    assert outcome.sha256 == disk_sha256


def test_write_preserves_existing_newline(tmp_path: Path):
    """T2: write('src/b.py', 'y = 2\\n', role='implementer') leaves content unchanged (exactly one newline)"""
    ledger = ArtifactLedger(None, "spec_t", tmp_path)
    ledger.write("src/b.py", "y = 2\n", role="implementer")

    file_content = (tmp_path / "src" / "b.py").read_bytes()
    expected_content = b"y = 2\n"
    assert file_content == expected_content
    # Ensure no extra newlines were added
    assert not file_content.endswith(b"\n\n")


def test_write_handles_empty_content(tmp_path: Path):
    """T3: write('empty.txt', '', role='implementer') produces zero-byte file"""
    ledger = ArtifactLedger(None, "spec_t", tmp_path)
    outcome = ledger.write("empty.txt", "", role="implementer")

    file_stat = (tmp_path / "empty.txt").stat()
    assert file_stat.st_size == 0
    # Should still have a sha256 entry even for empty files
    assert isinstance(outcome.sha256, str)


def test_restore_adds_trailing_newline(tmp_path: Path):
    """T4: restore('src/c.py', 'z = 3', role='implementer') produces b'z = 3\\n'; outcome sha256 matches on-disk bytes"""
    ledger = ArtifactLedger(None, "spec_t", tmp_path)
    outcome = ledger.restore("src/c.py", "z = 3", role="implementer")

    file_content = (tmp_path / "src" / "c.py").read_bytes()
    expected_content = b"z = 3\n"
    assert file_content == expected_content

    disk_sha256 = hashlib.sha256(file_content).hexdigest()
    assert outcome.sha256 == disk_sha256


def test_note_preserves_verbatim(tmp_path: Path):
    """T5: note('test_output.txt', '4 passed') writes exactly b'4 passed' without added newline"""
    ledger = ArtifactLedger(None, "spec_t", tmp_path)
    ledger.note("test_output.txt", "4 passed")

    file_content = (tmp_path / "test_output.txt").read_bytes()
    expected_content = b"4 passed"
    assert file_content == expected_content
    # Should not have a trailing newline
    assert not file_content.endswith(b"\n")


def test_read_entries_includes_correct_line_count_and_hash(tmp_path: Path):
    """T6: read_entries()[0].lines == 1 after T1; read_entries()[0].sha256 equals on-disk sha256"""
    ledger = ArtifactLedger(None, "spec_t", tmp_path)
    ledger.write("src/a.py", "x = 1", role="implementer")

    entries = read_entries(tmp_path)
    entry = entries[0]

    assert entry.lines == 1

    file_content = (tmp_path / "src" / "a.py").read_bytes()
    disk_sha256 = hashlib.sha256(file_content).hexdigest()
    assert entry.sha256 == disk_sha256