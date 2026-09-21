"""DEV-791: `#require` without `try`, and the macro-expansion diagnostic shape."""
from coding_model_autonomous import swift_prechecks as sp
from coding_model_autonomous import swift_rules as sr

RUN52_RETRY3_OUTPUT = """\
error: SwiftCompile normal arm64 /Users/km4/x/worktrees/spec_130f84de-ab6f04d4/Tests/CentipedeCoreTests/BoardSnapshotTests.swift failed with a nonzero exit code. Command line: cd /Users/km4
 64 |     let snap = g.boardSnapshot()

macro expansion #require:1:1: \x1b[1;31merror: \x1b[1;39mcall can throw but is not marked with 'try'\x1b[0;0m
`- /Users/km4/x/worktrees/spec_130f84de-ab6f04d4/Tests/CentipedeCoreTests/BoardSnapshotTests.swift:73:70: note: expanded code originates here
 73 |             #require(entity == .mushroom(damage: 0, poisoned: false))
    |             |- note: in expansion of macro 'require' here
error: Build failed
"""


def test_macro_expansion_error_is_located_at_the_originating_line():
    diags = sr.located_diagnostics(
        RUN52_RETRY3_OUTPUT, ["Tests/CentipedeCoreTests/BoardSnapshotTests.swift"])
    assert len(diags) == 1
    d = diags[0]
    assert d.located() == "Tests/CentipedeCoreTests/BoardSnapshotTests.swift:73"
    assert d.message == "call can throw but is not marked with 'try'"
    assert "try #require" in (sr.fix_hint(d.message) or "")


def test_require_without_try_is_flagged_and_try_forms_pass():
    src = """import Testing
@testable import CentipedeCore

@Test func a() throws {
    let x: Int? = 1
    #require(x == 1)
    let y = try #require(x)
    let z = try? #require(x)
    _ = try! #require(x)
    // #require(in a comment) does not count
    _ = (y, z)
}
"""
    v = sp.require_without_try([("Tests/T/ATests.swift", src)])
    assert [(x.kind, x.line) for x in v] == [("require_without_try", 6)]
    assert "call can throw but is not marked with 'try'" in v[0].message
    assert v[0].error_line().startswith("Tests/T/ATests.swift:6:1: error: ")


def test_require_precheck_is_wired_into_the_harness():
    res = sp.run_swift_prechecks([("Tests/T/B.swift", "@Test func b() throws {\n    #require(1 == 1)\n}\n")])
    assert any(x.kind == "require_without_try" for x in res.violations)
