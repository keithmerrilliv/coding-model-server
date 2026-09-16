"""Tests for count_test_declarations and declaration_delta helpers."""
import copy as _copy

from coding_model_autonomous.test_runner import (
    count_test_declarations,
    declaration_delta,
)


def test_count_pytest_basic():
    """Pytest counting works for basic cases."""
    src = "def test_foo(): pass"
    assert count_test_declarations(src, "pytest") == 1


def test_count_pytest_multiple():
    """Pytest counts multiple test functions correctly."""
    src = "\n".join([
        "def test_a(): pass",
        "def test_b(): pass",
        "def helper(): pass",  # not a test
        "x = 'def test_nope()'",  # string literal doesn't count
    ])
    assert count_test_declarations(src, "pytest") == 2


def test_count_pytest_indented():
    """Indented test functions are counted."""
    src = """
class TestSomething:
    def test_method(self):
        pass
        
    def helper(self):
        pass
"""
    assert count_test_declarations(src, "pytest") == 1


def test_count_pytest_commented_out_not_counted():
    """Commented-out test declarations don't count."""
    src = "# def test_x()\ndef test_y(): pass"
    assert count_test_declarations(src, "pytest") == 1


def test_count_swift_xctest():
    """Swift XCTest-style func testFoo( declarations are counted."""
    src = "\n".join([
        "func testBar() {}",
        "func testBaz() {}", 
        "func helper() {}",  # not a test
    ])
    assert count_test_declarations(src, "swift_test") == 2


def test_count_swift_testing_attribute():
    """@Test attribute followed by function is counted immediately."""
    src = "@Test\nfunc foo() {}"
    assert count_test_declarations(src, "swift_test") == 1


def test_count_swift_mixed():
    """Both XCTest and swift-testing forms are counted together.

    Against the design's own seam, 4 is the right answer for:
        @Test func foo() {}
        func testBar() {}
        @Test func baz() {}
        func testQuux() {}
    """
    src = "\n".join([
        "@Test func bar() {}",  # swift-testing with inline name
        "func testX() {}",      # XCTest
        "@Test\nfunc baz() {}", # swift-testing with separate line
        "func testY() {}",      # another XCTest
    ])
    assert count_test_declarations(src, "swift_test") == 4


def test_count_unknown_framework_returns_zero():
    """Unknown frameworks return zero without raising."""
    assert count_test_declarations("def test_x(): pass", "unknown") == 0
    assert count_test_declarations("", "pytest") == 0


def test_count_empty_source():
    """Empty or whitespace-only source returns zero for any framework."""
    assert count_test_declarations("", "pytest") == 0
    assert count_test_declarations("   ", "swift_test") == 0
    assert count_test_declarations("\t\n", "unknown") == 0


def test_delta_new_file_added():
    """New files in after contribute their full count."""
    before: dict[str, str] = {}
    after = {"a.py": "def test_a(): pass\ndef test_b(): pass"}
    
    delta = declaration_delta(before, after, "pytest")
    assert delta == {"a.py": 2}
    assert "a.py" in delta


def test_delta_existing_file_changed():
    """Changed file counts are reflected in the delta."""
    before = {"f.py": "def test_1():pass"}
    after = {"f.py": "def test_1():pass\ndef test_2():pass"}
    
    delta = declaration_delta(before, after, "pytest")
    assert delta == {"f.py": 1}


def test_delta_unchanged_not_in_result():
    """Unchanged paths are absent from the result.

    A path present in both maps with identical count must NOT appear.
    Only paths where (after_count - before_count) != 0 should be included.
    """
    # Case 1: a.py unchanged, b.py new with one test
    before = {"a.py": "def test_x():pass", "b.py": ""}
    after = {"a.py": "def test_x():pass", "b.py": "def test_y():pass"}
    
    delta = declaration_delta(before, after, "pytest")
    assert "a.py" not in delta  # unchanged
    assert delta == {"b.py": 1}  # new file contributes full count
    
    # Case 2: truly identical maps return empty dict (run 39 case)
    src = "def test_a(): pass"
    before2 = {"f.py": src}
    after2 = {"f.py": src}
    
    delta2 = declaration_delta(before2, after2, "pytest")
    assert delta2 == {}


def test_purity_double_call_equal():
    """Functions produce identical results on repeated calls."""
    src = "def test_x(): pass\ndef test_y(): pass"
    
    r1 = count_test_declarations(src, "pytest")
    r2 = count_test_declarations(src, "pytest")
    assert r1 == r2
    
    before = {"f.py": "def test_1():pass"}
    after = {"f.py": "def test_2():pass"}
    
    d1 = declaration_delta(before, after, "pytest")
    d2 = declaration_delta(before, after, "pytest")
    assert d1 == d2


def test_purity_no_mutation():
    """Arguments are not mutated by the functions."""
    src = "def test_x(): pass"
    original_src = _copy.deepcopy(src)
    
    count_test_declarations(src, "pytest")
    assert src == original_src
    
    before = {"f.py": "def test_1():pass"}
    after = {"g.py": "def test_2():pass"}
    b0 = _copy.deepcopy(before)
    a0 = _copy.deepcopy(after)
    
    declaration_delta(before, after, "pytest")
    assert before == b0
    assert after == a0


def test_count_pytest_multiline_string_not_counted():
    """Test declarations inside multiline strings don't count."""
    src = '''"""
def test_in_docstring():
    pass
"""'''
    assert count_test_declarations(src, "pytest") == 0


def test_count_swift_func_with_complex_name():
    """Swift func with complex test name pattern is counted."""
    src = """
func testFooBarBaz() {}
func testWithNumber123() {}
"""
    assert count_test_declarations(src, "swift_test") == 2


def test_self_import_root_is_correct():
    """The import root for tests is coding_model_autonomous (not src.)."""
    # This test verifies the module can be imported correctly from tests/
    from coding_model_autonomous.test_runner import count_test_declarations as ctd
    
    assert callable(ctd)
    assert ctd("def test_x(): pass", "pytest") == 1
