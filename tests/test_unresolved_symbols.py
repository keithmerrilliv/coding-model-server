"""DEV-698: a symbol the editable files call and nothing served defines.

Run 39's spec listed World.swift, Game.swift and GameTests.swift as editable
with ten protected paths. Game.tick() calls spawnWaveChain(), which lives in a
five-line Wave.swift that was neither editable, protected nor requested — so it
was never served, and context.json recorded `omitted: []`.

The architect said so in its own words — "But wait ... it calls
spawnWaveChain() which doesn't exist yet!" — and burned five attempts across
two rounds. The spec was cancelled. Resubmitting with Wave.swift added and
NOTHING else changed produced a valid design on the first attempt.
"""

import pytest

from coding_model_autonomous.context import SpecContext, unresolved_symbols
from coding_model_autonomous.executor import (
    build_architect_message,
    build_implementer_message,
)

GAME = """struct Game {
    mutating func tick() {
        world.step()
        spawnWaveChain()
        let p = Position(column: 1, row: 2)
    }
}
"""
POSITION = "struct Position { let column: Int; let row: Int }"
WAVE = "func spawnWaveChain() { }"


def test_run_39s_missing_symbol_is_reported():
    assert unresolved_symbols(
        {"Game.swift": GAME},
        {"Game.swift": GAME, "Position.swift": POSITION}) == ["spawnWaveChain"]


def test_serving_the_file_resolves_it():
    """The proof from the ticket: add Wave.swift, change nothing else."""
    assert unresolved_symbols(
        {"Game.swift": GAME},
        {"Game.swift": GAME, "Position.swift": POSITION,
         "Wave.swift": WAVE}) == []


def test_a_symbol_defined_in_a_protected_file_resolves():
    """Read-only references count as served — they are in the prompt."""
    assert unresolved_symbols({"Game.swift": GAME},
                              {"Game.swift": GAME, "Position.swift": POSITION,
                               "Wave.swift": WAVE}) == []


def test_a_method_on_a_receiver_is_not_reported():
    """world.step() resolves through a type we cannot see. Not our business."""
    assert "step" not in unresolved_symbols({"a.swift": GAME},
                                            {"a.swift": GAME})


@pytest.mark.parametrize("source", [
    "if (x) { return }",
    "for (i in 0..<3) { print(i) }",
    "let s = String(x); let n = Int(y)",
    "XCTAssertEqual(a, b)",
    "guard let v = v else { return }",
])
def test_control_flow_and_stdlib_are_not_reported(source):
    """The noise control. A flood of false positives is its own failure."""
    assert unresolved_symbols({"a.swift": source}, {"a.swift": source}) == []


def test_python_targets_work_too():
    src = "def run():\n    helper()\n    return build_thing()\n"
    assert unresolved_symbols({"m.py": src}, {"m.py": src}) == [
        "build_thing", "helper"]


def test_python_symbol_defined_elsewhere_resolves():
    src = "def run():\n    helper()\n"
    other = "def helper():\n    pass\n"
    assert unresolved_symbols({"m.py": src}, {"m.py": src, "u.py": other}) == []


# ── it reaches the event and the prompt ────────────────────────────────────

def test_the_event_payload_carries_it():
    ctx = SpecContext.from_files(
        "s1", editable=[("Game.swift", GAME)],
        protected=[("Position.swift", POSITION)])
    assert ctx.summary()["unresolved"] == ["spawnWaveChain"]


def test_the_event_payload_omits_the_key_when_everything_resolves():
    """Negative control — no noise on a healthy context."""
    ctx = SpecContext.from_files(
        "s1", editable=[("Game.swift", GAME)],
        protected=[("Position.swift", POSITION), ("Wave.swift", WAVE)])
    assert "unresolved" not in ctx.summary()


def test_the_architect_is_told_what_it_cannot_see():
    msgs = build_architect_message(
        "# Spec", existing_files=[("Game.swift", GAME)],
        unresolved=["spawnWaveChain"])
    body = "\n".join(m["content"] for m in msgs)
    assert "Referenced but not shown" in body
    assert "spawnWaveChain" in body
    assert "do NOT" in body and "invent a definition" in body


def test_the_architect_prompt_is_unchanged_when_nothing_is_unresolved():
    """Negative control: a healthy context costs no prompt budget."""
    msgs = build_architect_message(
        "# Spec", existing_files=[("Game.swift", GAME)], unresolved=[])
    assert "Referenced but not shown" not in "\n".join(
        m["content"] for m in msgs)


# ── DEV-698 follow-up: a long list is signal, not confusion ────────────────

def test_a_long_list_is_reported_not_suppressed():
    """Electric Sheep run 1 disproved the original premise.

    The first version stood down above six candidates, assuming a long list
    meant the heuristic was confused. Run 1 served three editable files and
    ZERO protected files, and produced 17 candidates of which eleven were real
    project types the architect could not see. A long list meant the context
    was very short — the case the check exists for, silenced by its own cap.
    """
    editable = {"a.swift": "\n".join(
        f"    _ = Thing{i}()" for i in range(12))}
    names = unresolved_symbols(editable, editable)
    assert len(names) == 12, names
    assert "Thing0" in names and "Thing11" in names


def test_an_absurd_list_still_stands_down():
    """The ceiling that really does mean confusion is still there."""
    editable = {"a.swift": "\n".join(
        f"    _ = Thing{i}()" for i in range(60))}
    assert unresolved_symbols(editable, editable) == []


@pytest.mark.parametrize("name", [
    "MLXArray", "softmax", "CACurrentMediaTime", "CompositorLayer",
    "ImmersiveSpace", "WindowGroup", "DispatchQueue",
])
def test_framework_names_from_the_first_real_run_are_filtered(name):
    """These came out of Electric Sheep run 1 and are not repository symbols."""
    src = f"func go() {{ _ = {name}() }}"
    assert name not in unresolved_symbols({"a.swift": src}, {"a.swift": src})


def test_the_prompt_truncates_a_long_list_and_says_how_many_more():
    msgs = build_architect_message(
        "# Spec", existing_files=[("a.swift", GAME)],
        unresolved=[f"Thing{i}" for i in range(25)])
    body = "\n".join(m["content"] for m in msgs)
    assert "and 5 more" in body
    assert "Thing0" in body


def test_the_prompt_no_longer_claims_they_are_all_in_the_repository():
    """MLXArray is not in the repo. The old wording asserted it was."""
    msgs = build_architect_message(
        "# Spec", existing_files=[("a.swift", GAME)], unresolved=["Thing"])
    body = "\n".join(m["content"] for m in msgs)
    assert "or in a framework" in body


# ── DEV-698: the IMPLEMENTER is the role that meets the compiler ───────────

def _impl(unresolved):
    return "\n".join(m["content"] for m in build_implementer_message(
        "# Spec", "# Design", existing_files=[("a.swift", GAME)],
        unresolved=unresolved))


def test_the_implementer_is_told_what_it_cannot_see():
    """spec_0aab1c17 died here, not at the architect.

    It needed an audio spy, subclassed `AudioManager` without being served the
    file, and `AudioManager` is `final`. Five attempts, a synthesis pass and a
    repair pass, all on the shape of a type it could not read. The list existed
    the whole time and only the architect got it.
    """
    body = _impl(["AudioManager", "ForcingStrategy", "Particle"])
    assert "Referenced but NOT shown" in body
    assert "AudioManager" in body


def test_the_implementer_is_warned_off_subclassing_what_it_cannot_read():
    """The specific mistake that killed the spec."""
    body = _impl(["AudioManager"])
    assert "Do NOT subclass one" in body
    assert "final" in body
    assert "inject a closure" in body


def test_a_healthy_context_costs_the_implementer_no_prompt():
    """Negative control."""
    assert "Referenced but NOT shown" not in _impl([])
    assert "Referenced but NOT shown" not in _impl(None)


def test_the_implementer_list_truncates_like_the_architect_one():
    body = _impl([f"Thing{i}" for i in range(25)])
    assert "and 5 more" in body


# ── DEV-775: a symbol the imported module provides is not unresolved ─────────

RUN49_PACKAGE = """// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "Centipede",
    platforms: [.macOS(.v15)],
    targets: [
        .target(name: "CentipedeRender"),
        .testTarget(name: "CentipedeRenderTests", dependencies: ["CentipedeRender"]),
    ]
)
"""


def test_package_swift_reports_nothing_dev775():
    from coding_model_autonomous.context import unresolved_symbols
    assert unresolved_symbols({"Package.swift": RUN49_PACKAGE},
                              {"Package.swift": RUN49_PACKAGE}) == []


def test_module_allowance_is_scoped_to_the_importing_file_dev775():
    from coding_model_autonomous.context import unresolved_symbols
    # Run 42's shape stays reported: a repository type nothing served declares.
    app = "import SwiftUI\nlet b = MetricsParticleBridge()\nlet p = Package()\n"
    got = unresolved_symbols({"App.swift": app}, {"App.swift": app})
    assert "MetricsParticleBridge" in got
    assert "Package" in got          # no PackageDescription import here


# ── DEV-698 false positives, each shape taken from a recorded CONTEXT_ASSEMBLED ─

def _only(source: str) -> list:
    return unresolved_symbols({"F.swift": source}, {"F.swift": source})


AUDIOSCAPE = """import AVFoundation

final class Audioscape {
    let engine = AVAudioEngine()
    func build() {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)
        let node = AVAudioSourceNode { _, _, _, list in
            let buffers = UnsafeMutableAudioBufferListPointer(list)
            let v = sinf(0.5) * expf(-1) + sqrtf(2)
            return noErr
        }
        let reverb = AVAudioUnitReverb()
    }
}
"""


def test_avfoundation_libm_and_pointer_types_are_not_missing_files():
    """Runs 43-46 and 50-51: eight names reported on every audio dispatch."""
    assert _only(AUDIOSCAPE) == []


def test_framework_names_without_the_import_are_still_reported():
    """Control: the allowance is the import's, not the name's."""
    got = _only("let e = AVAudioEngine()\nlet v = sinf(0.5)\n")
    assert "AVAudioEngine" in got and "sinf" in got


def test_metal_names_int32_and_memcpy_are_not_missing_files():
    """Runs 46-47 (HalluRenderer)."""
    src = ("import Metal\nimport simd\n"
           "let d = MTLCreateSystemDefaultDevice()\n"
           "let p = MTLRenderPipelineDescriptor()\n"
           "let m = simd_float4x4(1)\nlet n = Int32(4)\nmemcpy(dst, src, 16)\n")
    assert _only(src) == []


def test_an_enum_case_with_an_associated_value_is_a_declaration():
    """`case downloading(progress: Double)` read as a call to a missing
    `downloading` on runs 55, 60, 61 and 62."""
    src = ("enum ModelState {\n    case idle\n    case downloading(progress: Double)\n"
           "    case error(String)\n}\n")
    assert _only(src) == []


def test_attributes_are_not_calls():
    src = ('import Testing\n@Suite("s") struct S {\n'
           '    @Test("t") func t() {}\n'
           '    func go(completion: @escaping () -> Void) { completion() }\n}\n')
    assert _only(src) == []


def test_swiftui_environment_actions_and_closure_parameters_resolve():
    """Run 61's ContentView: `dismiss()`, `openImmersiveSpace()` and a
    closure parameter `pick` called in the body."""
    src = ("import SwiftUI\nstruct V: View {\n"
           "    @Environment(\\.dismiss) private var dismiss\n"
           "    @Environment(\\.openImmersiveSpace) var openImmersiveSpace\n"
           "    func choose(pick: @escaping () -> Int, onPick: (Int) -> Void) {\n"
           "        onPick(pick()); dismiss()\n    }\n"
           "    var body: some View { VStack { Button(\"x\") {} ; Toggle(\"t\", isOn: .constant(true)) } }\n}\n")
    assert _only(src) == []


def test_real_repository_symbols_survive_the_filters():
    """Control: run 61's true positives, in a file that imports SwiftUI and
    MLX, stay reported."""
    src = ("import SwiftUI\nimport MLXLMCommon\n"
           "let c = ModelConfiguration(id: \"x\")\n"
           "let v = MetalViewRepresentable()\nlet g = GenerationTaskController()\n")
    assert _only(src) == ["GenerationTaskController", "MetalViewRepresentable"]
