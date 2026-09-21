"""DEV-777 (v0.4.0 phase A1): the runs-44–49 Swift precheck classes.

Each detector is pinned in both directions against the shape of the run that
paid a Mac round trip for it (fixtures are excerpts of the real artifacts in
var/tasks_db/specs), plus a delivered-artifact shape as the negative control.
The archive sweep (scripts/sweep_swift_prechecks.py) is the population-level
check; these are the unit contracts.
"""
from coding_model_autonomous import swift_prechecks as sp


# ── class 1: `throws` missing on a test that uses `try` (run 48) ─────────────

RUN48_TEST = """import Testing
@testable import CentipedeRender

@Suite struct OffscreenRendererTests {

    @Test func criterion2_emptyFrame() {
        let renderer = try #require(OffscreenRenderer())
        let frame = try #require(renderer.render(FrameSnapshot(), width: 300, height: 300))
        #expect(frame.width == 300)
    }

    @Test func criterion3_oneMushroomExtent() {
        let renderer = try #require(OffscreenRenderer())
        #expect(renderer != nil)
    }
}
"""


def test_run48_shape_fires_once_per_test_function():
    vs = sp.missing_throws_on_test_functions([("Tests/T.swift", RUN48_TEST)])
    assert [v.kind for v in vs] == ["missing_throws_on_test"] * 2
    assert vs[0].line == 7 and "criterion2_emptyFrame" in vs[0].message
    assert vs[1].line == 13
    assert "errors thrown from here are not handled" in vs[0].message


def test_throws_on_signature_is_clean():
    src = RUN48_TEST.replace("criterion2_emptyFrame() {", "criterion2_emptyFrame() throws {") \
                    .replace("criterion3_oneMushroomExtent() {", "criterion3_oneMushroomExtent() async throws {")
    assert sp.missing_throws_on_test_functions([("Tests/T.swift", src)]) == []


def test_xctest_naming_counts_as_a_test():
    src = ("import XCTest\nfinal class T: XCTestCase {\n"
           "    func testA() {\n        let x = try make()\n        XCTAssertNotNil(x)\n    }\n}\n")
    vs = sp.missing_throws_on_test_functions([("T.swift", src)])
    assert len(vs) == 1 and vs[0].line == 4


def test_try_bang_try_question_do_block_and_closures_do_not_fire():
    src = ("import Testing\n@Suite struct S {\n"
           "    @Test func a() { let x = try? make(); #expect(x != nil) }\n"
           "    @Test func b() { let x = try! make(); #expect(x != nil) }\n"
           "    @Test func c() { do { _ = try make() } catch { } }\n"
           "    @Test func d() { let f = { try make() }; _ = f }\n"
           "}\n")
    assert sp.missing_throws_on_test_functions([("T.swift", src)]) == []


def test_non_test_function_never_fires():
    src = "struct S {\n    func helper() {\n        _ = try make()\n    }\n}\n"
    assert sp.missing_throws_on_test_functions([("S.swift", src)]) == []


# ── class 2: Hashable conformance (run 48) ───────────────────────────────────

RUN48_SOURCE = """import Foundation
public struct RGBA: Equatable, Sendable {
    public var r, g, b, a: UInt8
}
public enum Palette {
    public static let mushroom: [RGBA] = []
}
"""


def test_set_of_declared_struct_without_hashable_fires_through_array_member():
    tests = "@Suite struct T {\n    @Test func x() throws {\n        #expect(Set(Palette.mushroom).count == 4)\n    }\n}\n"
    vs = sp.missing_hashable_conformance([("S.swift", RUN48_SOURCE), ("T.swift", tests)])
    assert len(vs) == 1
    assert vs[0].path == "T.swift" and vs[0].line == 3
    assert "requires that 'RGBA' conform to 'Hashable'" in vs[0].message
    assert ("S.swift", 2) in vs[0].notes


def test_set_generic_and_dictionary_key_forms():
    tests = "let a: Set<RGBA> = []\nlet b: [RGBA: Int] = [:]\n"
    vs = sp.missing_hashable_conformance([("S.swift", RUN48_SOURCE), ("T.swift", tests)])
    assert [v.line for v in vs] == [1]          # once per (type, file)


def test_hashable_in_declaration_or_extension_is_clean():
    decl = RUN48_SOURCE.replace("Equatable, Sendable", "Hashable, Sendable")
    ext = RUN48_SOURCE + "extension RGBA: Hashable {}\n"
    tests = "let a: Set<RGBA> = []\n"
    assert sp.missing_hashable_conformance([("S.swift", decl), ("T.swift", tests)]) == []
    assert sp.missing_hashable_conformance([("S.swift", ext), ("T.swift", tests)]) == []


def test_enums_and_undeclared_types_do_not_fire():
    src = "enum Kind { case a, b }\nlet s: Set<Kind> = []\nlet t: Set<GridPosition> = []\n"
    assert sp.missing_hashable_conformance([("S.swift", src)]) == []


# ── class 3: nil for a non-optional argument (run 49 retry 1) ────────────────

def test_run49_makebuffer_options_nil_fires():
    src = ("let vertexBuffer = device.makeBuffer(bytes: vertices, "
           "length: MemoryLayout<Vertex>.stride * vertices.count, options: nil)!\n")
    vs = sp.nil_for_non_optional_argument([("R.swift", src)])
    assert len(vs) == 1 and vs[0].line == 1
    assert "'MTLResourceOptions'" in vs[0].message


def test_makebuffer_with_empty_options_is_clean():
    src = "let b = device.makeBuffer(length: 16, options: [])\n"
    assert sp.nil_for_non_optional_argument([("R.swift", src)]) == []


def test_emitted_signature_non_optional_param_fires_and_optional_does_not():
    src = ("struct Renderer {\n    func draw(count: Int, label: String?) {}\n}\n"
           "func use(r: Renderer) {\n    r.draw(count: nil, label: nil)\n}\n")
    vs = sp.nil_for_non_optional_argument([("R.swift", src)])
    assert len(vs) == 1 and "'count'" in vs[0].message and "'Int'" in vs[0].message


def test_overloaded_or_defaulted_params_stay_silent():
    src = ("func f(x: Int = 1) {}\nfunc g(x: Int) {}\nfunc g(x: String) {}\n"
           "f(x: nil)\ng(x: nil)\n")
    assert sp.nil_for_non_optional_argument([("R.swift", src)]) == []


# ── class 5: @MainActor type built from a nonisolated test (runs 44, 47) ─────

RUN44_SOURCE = """import Foundation

@MainActor
final class AudioLifecycle {
    init() {}
    func process(_ e: Int) -> Int { e }
}
"""
RUN44_TEST = """import XCTest
@testable import ElectricSheep

final class AudioLifecycleTests: XCTestCase {

    func testPlayInterruptionBeganStopsEngine() {
        let lc = AudioLifecycle()
        _ = lc.process(1)
    }

    func testSecond() {
        let lc = AudioLifecycle()
        _ = lc.process(2)
    }
}
"""


def test_run44_shape_fires_per_test_function():
    vs = sp.main_actor_types_called_from_nonisolated_tests(
        [("ElectricSheep/AudioLifecycle.swift", RUN44_SOURCE),
         ("ElectricSheepTests/AudioLifecycleTests.swift", RUN44_TEST)])
    assert [v.line for v in vs] == [7, 12]
    assert "main actor-isolated initializer 'init' of 'AudioLifecycle'" in vs[0].message
    assert ("ElectricSheep/AudioLifecycle.swift", 4) in vs[0].notes


def test_main_actor_on_func_class_or_async_is_clean():
    on_func = RUN44_TEST.replace("    func testPlayInterruptionBeganStopsEngine() {",
                                 "    @MainActor func testPlayInterruptionBeganStopsEngine() async throws {") \
                        .replace("    func testSecond() {", "    @MainActor func testSecond() async {")
    on_class = RUN44_TEST.replace("final class AudioLifecycleTests", "@MainActor final class AudioLifecycleTests")
    async_only = RUN44_TEST.replace("() {", "() async {")
    for variant in (on_func, on_class, async_only):
        assert sp.main_actor_types_called_from_nonisolated_tests(
            [("A.swift", RUN44_SOURCE), ("T.swift", variant)]) == []


def test_type_without_main_actor_is_clean():
    plain = RUN44_SOURCE.replace("@MainActor\n", "")
    assert sp.main_actor_types_called_from_nonisolated_tests(
        [("A.swift", plain), ("T.swift", RUN44_TEST)]) == []


# ── the whole set: run 49's delivered shape stays clean ──────────────────────

RUN49_DELIVERED_TEST = """import Testing
@testable import CentipedeRender

@Suite struct OffscreenRendererTests {
    @Test func criterion2_emptyFrame() throws {
        let renderer = try #require(OffscreenRenderer())
        let frame = try #require(renderer.render(FrameSnapshot(), width: 300, height: 300))
        #expect(frame.width == 300)
    }
    @Test func distinctColours() throws {
        #expect(Set(Palette.mushroom).count == 4)
    }
}
"""
RUN49_DELIVERED_SOURCE = """import Metal
public struct RGBA: Hashable, Sendable { public var r, g, b, a: UInt8 }
public enum Palette { public static let mushroom: [RGBA] = [] }
public final class OffscreenRenderer {
    public static let tile = 10
    public init?(device: MTLDevice? = nil) { return nil }
    func build(device: MTLDevice, vertices: [Float]) {
        _ = device.makeBuffer(length: vertices.count, options: [])
        _ = Self.tile
    }
}
"""


def test_delivered_run49_shape_is_clean_through_the_public_entry_point():
    result = sp.run_swift_prechecks([("Sources/R.swift", RUN49_DELIVERED_SOURCE),
                                     ("Tests/T.swift", RUN49_DELIVERED_TEST)])
    assert not result.failed(), result.report()


def test_public_entry_point_reports_every_new_class_in_swiftc_shape():
    result = sp.run_swift_prechecks([("S.swift", RUN48_SOURCE), ("T.swift", RUN48_TEST),
                                     ("A.swift", RUN44_SOURCE), ("AT.swift", RUN44_TEST)])
    kinds = {v.kind for v in result.violations}
    assert {"missing_throws_on_test", "main_actor_call_from_nonisolated_test"} <= kinds
    for line in result.report().splitlines():
        assert ": error: " in line or ": note: " in line
    payload = result.event_payload()
    assert all({"kind", "path", "line", "message", "related"} <= set(p) for p in payload)
