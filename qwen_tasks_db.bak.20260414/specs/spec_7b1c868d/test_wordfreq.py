"""Pytest test suite for word frequency counter."""

import os
import subprocess
import sys
import tempfile
import pytest


def run_wordfreq(args, input_text=None):
    """Run wordfreq CLI and return stdout, stderr, exit code."""
    cmd = [sys.executable, "wordfreq.py"] + args
    
    if input_text is not None:
        # Write input to a temp file
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write(input_text)
            temp_path = f.name
        try:
            cmd.append(temp_path)
            result = subprocess.run(
                cmd, capture_output=True, text=True
            )
            return result.stdout, result.stderr, result.returncode
        finally:
            os.unlink(temp_path)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode


def test_basic_word_count():
    """Test basic word counting."""
    content = "hello world hello"
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 2
    assert "2 hello" in lines[0]
    assert "1 world" in lines[1]


def test_case_insensitive():
    """Test that word counting is case-insensitive."""
    content = "Hello HELLO hello World WORLD"
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 2
    assert "3 hello" in lines[0]
    assert "2 world" in lines[1]


def test_punctuation_stripped():
    """Test that punctuation is stripped from words."""
    content = "hello, world! hello; world? hello."
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 2
    assert "3 hello" in lines[0]
    assert "2 world" in lines[1]


def test_empty_file():
    """Test handling of empty files."""
    content = ""
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    assert stdout.strip() == ""


def test_missing_file():
    """Test error handling for missing file."""
    _, stderr, code = run_wordfreq(["nonexistent_file.txt"])
    
    assert code == 1
    assert "does not exist" in stderr


def test_default_top_value():
    """Test that default --top value is 10."""
    # Create content with at least 10 different words
    words = " ".join([f"word{i}" for i in range(20)])
    stdout, stderr, code = run_wordfreq([], words)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 10


def test_negative_top_value():
    """Test handling of negative --top value."""
    content = "hello world"
    _, stderr, code = run_wordfreq(["--top", "-1"], content)
    
    assert code == 1
    assert "non-negative" in stderr


def test_zero_top_value():
    """Test handling of zero --top value."""
    content = "hello world hello"
    stdout, stderr, code = run_wordfreq(["--top", "0"], content)
    
    assert code == 0
    assert stdout.strip() == ""


def test_top_greater_than_words():
    """Test when --top is greater than number of unique words."""
    content = "hello world hello"
    stdout, stderr, code = run_wordfreq(["--top", "100"], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    # Should only return the actual unique words (2)
    assert len(lines) == 2


def test_special_punctuation_chars():
    """Test handling of various punctuation characters."""
    content = "hello... world!!! hello? world' world\" world(world)"
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 2
    assert "2 hello" in lines[0]
    assert "3 world" in lines[1]


def test_unicode_words():
    """Test handling of Unicode characters in words."""
    content = "héllo world héllo café"
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) >= 2
    # Check that Unicode words are counted correctly
    assert any("héllo" in line for line in lines)


def test_multiple_newlines_and_whitespace():
    """Test handling of multiple newlines and whitespace."""
    content = """hello
    
    world
    hello
    world"""
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 2
    assert "2 hello" in lines[0]
    assert "2 world" in lines[1]


def test_single_word():
    """Test with a single word."""
    content = "hello"
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 1
    assert "1 hello" in lines[0]


def test_no_words():
    """Test with text containing no words (only punctuation)."""
    content = "... !!! ??? ---"
    stdout, stderr, code = run_wordfreq([], content)
    
    assert code == 0
    assert stdout.strip() == ""