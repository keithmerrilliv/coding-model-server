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
from coding_model_autonomous.executor import build_architect_message

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
