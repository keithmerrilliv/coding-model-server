"""DEV-833: missing_equatable ignores type names inside string literals.

Run 68 (spec_9decc8db, LLab DEV-832) seam C7 asserts
`#expect(nsErr.domain == "USDSceneParserError")` — two Strings. The type name
appears only inside the literal, because an NS_ERROR_ENUM's domain string
conventionally IS the type's name, and the check read it as comparing
`USDSceneParserError` values.
"""

from fixture_files import load_fixture
from coding_model_autonomous import design_testability as dt

DESIGN = load_fixture("dev833_run68_round1_design.md")


def _equatable_findings(design_md: str) -> list:
    return [f for f in dt.check_design_testability(design_md)
            if f.kind == dt.KIND_MISSING_EQUATABLE]


def test_run68_round1_domain_string_compare_raises_no_equatable_finding():
    assert _equatable_findings(DESIGN) == []


def test_type_name_in_a_string_literal_is_not_a_comparison():
    types = {"USDSceneParserError"}
    assert dt._compared_types(
        '`#expect(nsErr.domain == "USDSceneParserError")`', types, {}) == set()
    assert dt._compared_types(
        r'`#expect(msg == "a \"USDSceneParserError\" b")`', types, {}) == set()


def test_type_named_outside_the_quotes_still_counts():
    """Negative control: a real comparison of the type still resolves."""
    types = {"USDSceneParserError"}
    assert dt._compared_types(
        '`#expect(err == USDSceneParserError(.notUSDA))`', types, {}) == types
    assert dt._compared_types(
        '`#expect(label(of: USDSceneParserError.self) == "x")`',
        types, {}) == types
