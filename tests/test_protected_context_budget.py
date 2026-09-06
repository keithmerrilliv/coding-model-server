"""DEV-627: the protected reference context has its own budget knob.

It used to spend EXISTING_FILES_MAX_CHARS — the same knob as the DEV-571
existing-files section — so run 20's 450K operator override silently let a
420K-char protected tree into run 21's implementer prompt, and the model
server refused the >1MB body with a 413 (terminal via DEV-624).
"""
from unittest import mock

import coding_model_autonomous.executor as ex


def test_reference_budget_is_not_the_existing_files_knob():
    """The regression itself: inflating the existing-files budget must not
    inflate the reference render."""
    big = "x" * 10_000
    with mock.patch.object(ex, "EXISTING_FILES_MAX_CHARS", 1_000_000), \
         mock.patch.object(ex, "PROTECTED_FILES_MAX_CHARS", 100):
        out = ex._render_reference_files([("src/big.py", big)])
    assert big not in out
    assert "src/big.py" in out  # named in the Not-shown listing
    assert "Not shown" in out


def test_files_within_budget_render_in_full():
    with mock.patch.object(ex, "PROTECTED_FILES_MAX_CHARS", 100):
        out = ex._render_reference_files([("src/small.py", "TINY = 1")])
    assert "TINY = 1" in out
    assert "Not shown" not in out


def test_budget_spends_across_files_and_names_every_omission():
    """First file consumes the budget; both later files are omitted but both
    stay named as off-limits."""
    with mock.patch.object(ex, "PROTECTED_FILES_MAX_CHARS", 50):
        out = ex._render_reference_files([
            ("src/a.py", "a" * 40),
            ("src/b.py", "b" * 40),
            ("src/c.py", "c" * 40),
        ])
    assert "a" * 40 in out
    assert "b" * 40 not in out and "c" * 40 not in out
    assert "src/b.py" in out and "src/c.py" in out


def test_all_omitted_still_carries_the_protection_instruction():
    """A tiny budget drops all ballast but the render must still tell the
    model the paths exist and are off-limits — that is the DEV-492 point."""
    with mock.patch.object(ex, "PROTECTED_FILES_MAX_CHARS", 10):
        out = ex._render_reference_files([("src/huge.py", "z" * 1_000)])
    assert "may NOT change" in out
    assert "src/huge.py" in out
    assert "z" * 1_000 not in out
