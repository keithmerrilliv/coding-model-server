"""The Python pack: pytest test counting, and the seam-import design rule."""
from __future__ import annotations

import re

from .base import RULE_SEAM_IMPORTS, LanguagePack


_PYTEST_TEST_RE = re.compile(r'^\s*def\s+test_\w+\s*\(')


def _count_pytest_tests(source: str) -> int:
    """Count pytest-style test functions in Python source."""
    count = 0
    in_multiline_string = False
    
    for line in source.splitlines():
        stripped = line.lstrip()
        
        # Skip comment lines entirely
        if stripped.startswith('#'):
            continue
        
        # Handle triple-quote state tracking
        if '"""' in line or "'''" in line:
            # Count occurrences of triple quotes on this line
            double_quotes = line.count('"""')
            single_quotes = line.count("'''")
            
            # If odd number of triple quotes, toggle state
            total_triple_quotes = double_quotes + single_quotes
            if total_triple_quotes % 2 == 1:
                in_multiline_string = not in_multiline_string
            
            # If we're now inside a multiline string, skip this line
            if in_multiline_string:
                continue
        
        # Skip if currently inside a multiline string
        if in_multiline_string:
            continue
        
        # Check for pytest test pattern
        if _PYTEST_TEST_RE.match(line):
            count += 1
    
    return count


class PythonPack(LanguagePack):
    name = "python"
    frameworks = frozenset({"pytest"})
    # A Python seam must import the code under test (DEV-661); Swift has
    # module-level visibility and no import in a seam.
    design_rules = frozenset({RULE_SEAM_IMPORTS})

    def count_tests(self, source: str) -> int:
        return _count_pytest_tests(source)
