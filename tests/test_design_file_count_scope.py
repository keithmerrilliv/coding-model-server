"""DEV-643: the design file count reads the File Structure, not the whole document.

Written by run 25 (spec_bda68e98) as far as the module goes; the test
expectations here correct the spec's own error (the path regex has no txt
extension, so a six-path fixture with two txt paths counts four).
"""
from __future__ import annotations

import pytest

from coding_model_autonomous import executor
from coding_model_autonomous.executor import (
    _file_structure_section, estimate_design_file_count,
    estimate_design_unit_count, use_manifest_mode,
)

SCOPED = """# Architecture: Scoped count

## Overview
Two files change.

## File Structure
```text
src/pkg/alpha.py        # Modified
tests/test_alpha.py     # New
```

## Criterion Seams
- C1 | setup: `design = SCOPED` | act: `estimate_design_file_count(design)` | assert: `== 2`
- fixtures name src/a.py, src/b.py, src/c.py, empty.txt, test_output.txt and other/d.py
"""

FLAT = SCOPED.replace("## File Structure\n```text\nsrc/pkg/alpha.py        # Modified\n"
                      "tests/test_alpha.py     # New\n```\n\n", "")


class TestScopedCount:
    def test_c1_scoped_to_the_file_structure(self):
        assert estimate_design_file_count(SCOPED) == 2

    def test_c2_no_file_structure_falls_back_to_the_whole_document(self):
        # src/a.py, src/b.py, src/c.py, other/d.py — the two .txt paths do not
        # match _DESIGN_FILE_PATH_RE (no txt extension), so 4, not 6.
        assert "## File Structure" not in FLAT
        assert estimate_design_file_count(FLAT) == 4

    def test_c3_scoped_design_stays_single_call(self, monkeypatch):
        monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "auto")
        monkeypatch.setattr(executor, "MANIFEST_FILE_THRESHOLD", 8)
        assert use_manifest_mode(SCOPED) is False

    def test_unit_count_is_scoped_the_same_way(self):
        """use_manifest_mode's second signal must not undo the first."""
        assert estimate_design_unit_count(SCOPED) == 2
        assert estimate_design_unit_count(FLAT) >= 4

    def test_c4_section_without_paths_falls_back(self):
        design = ("## File Structure\n```text\nsrc/\ntests/\n```\n\n"
                  "## Prose\nTouches src/x.py, src/y.py and src/z.py.\n")
        assert estimate_design_file_count(design) == 3

    def test_c5_boundary_is_the_next_level_two_heading(self):
        design = ("## File Structure\nincluded.py\n### Notes\nalso_included.py\n"
                  "## Next Section\nexcluded.py\n")
        section = _file_structure_section(design)
        assert "also_included.py" in section and "excluded.py" not in section
        assert estimate_design_file_count(design) == 2

    @pytest.mark.parametrize("value", ["", None])
    def test_c6_empty_inputs(self, value):
        assert estimate_design_file_count(value) == 0

    def test_run_24_shape_counts_two_not_nine(self):
        """The design that sent run 24 into manifest mode."""
        design = SCOPED.replace(
            "- fixtures name src/a.py, src/b.py, src/c.py, empty.txt, test_output.txt and other/d.py",
            "- T1 | `ledger.write(\"src/a.py\", ...)`\n- T2 | `src/b.py`\n- T3 | `empty.txt`\n"
            "- T4 | `src/c.py`\n- T5 | `test_output.txt`\n- T6 | `src/a.py` again, `other/d.py`, `more/e.py`, `more/f.py`")
        assert estimate_design_file_count(design) == 2
