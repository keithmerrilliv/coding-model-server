"""Swift test declarations, counted statically (DEV-700)."""
from __future__ import annotations

import re


# DEV-751: XCTest discovers ANY method whose name begins with `test` —
# `testFoo` and `test_foo` both run. The former `test[A-Z]` form read run 44's
# twelve snake_case tests as zero and under-counted 37 of 386 archived files.
_SWIFT_FUNC_TEST_RE = re.compile(r'\bfunc\s+test\w*\s*\(')
_SWIFT_ATTRIBUTE_RE = re.compile(r'^\s*@Test\b')
_FUNC_RE = re.compile(r'\bfunc\b')


def count_swift_tests(source: str) -> int:
    """Count Swift test functions (XCTest and swift-testing).

    Each `@Test` attribute is one test, and so is each XCTest-style
    `func test…(`. An `@Test` owns the next `func` declaration, on its own
    line or a later one (`@Test` / `@MainActor` / `func testFoo()`), so a
    swift-testing test whose name happens to start with `test` counts once,
    not twice (DEV-911). `//` lines are skipped.
    """
    count = 0
    # True from an `@Test` line until the `func` it annotates.
    owned = False
    for line in source.splitlines():
        if line.lstrip().startswith('//'):
            continue
        if _SWIFT_ATTRIBUTE_RE.match(line):
            count += 1
            owned = not _FUNC_RE.search(line)
            continue
        if _SWIFT_FUNC_TEST_RE.search(line) and not owned:
            count += 1
        if _FUNC_RE.search(line):
            owned = False
    return count
