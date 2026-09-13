"""Tests for is_placeholder_path Rules 6-7 (DEV-655)."""
from coding_model_autonomous.workspace import is_placeholder_path


def test_format_placeholders():
    """T1-T3: paths containing { or } are placeholders."""
    assert is_placeholder_path("{p}") is True
    assert is_placeholder_path("{path}") is True
    assert is_placeholder_path("{file}.py") is True
    assert is_placeholder_path("src/a{b}.py") is True


def test_glob_metacharacters():
    """T4-T6: paths containing *, ?, or [ are placeholders."""
    assert is_placeholder_path("test_*.py") is True
    assert is_placeholder_path("src/*.py") is True
    assert is_placeholder_path("src/x?.py") is True
    assert is_placeholder_path("src/[abc].py") is True


def test_existing_vocabulary_still_refuses():
    """T7: existing vocabulary still returns True."""
    assert is_placeholder_path("path/to/File.ext") is True
    assert is_placeholder_path("...") is True
    assert is_placeholder_path("") is True


def test_real_paths_still_pass():
    """T8: real file paths return False."""
    assert is_placeholder_path("src/coding_model_autonomous/executor.py") is False
    assert is_placeholder_path("tests/test_x.py") is False
    assert is_placeholder_path("src/coding_model_autonomous/__init__.py") is False
