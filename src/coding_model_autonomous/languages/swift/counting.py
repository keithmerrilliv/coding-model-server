"""Swift test declarations, counted statically (DEV-700)."""
from __future__ import annotations

import re


# DEV-751: XCTest discovers ANY method whose name begins with `test` —
# `testFoo` and `test_foo` both run. The former `test[A-Z]` form read run 44's
# twelve snake_case tests as zero and under-counted 37 of 386 archived files.
_SWIFT_FUNC_TEST_RE = re.compile(r'\bfunc\s+test\w*\s*\(')
_SWIFT_ATTRIBUTE_RE = re.compile(r'^\s*@Test\b')


def count_swift_tests(source: str) -> int:
    """Count Swift test functions (XCTest and swift-testing).

    Counts @Test attribute lines immediately without lookahead. Each @Test
    contributes exactly 1. Also counts func testFoo( declarations separately.
    A line matching both (@Test func testFoo()) counts as ONE, not two.
    """
    count = 0
    lines = source.splitlines()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Skip comment lines
        stripped = line.lstrip()
        if stripped.startswith('//'):
            i += 1
            continue
        
        # Check for @Test attribute - count it immediately on its own line
        if _SWIFT_ATTRIBUTE_RE.match(line):
            count += 1
            i += 1
            continue
        
        # Check for XCTest-style func testFoo( pattern
        if _SWIFT_FUNC_TEST_RE.search(line):
            count += 1
        
        i += 1
    
    return count
