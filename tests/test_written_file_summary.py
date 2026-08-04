"""Sibling-interface summary must actually describe the interface — DEV-467.

Manifest mode generates one file per agent call and hands each call a summary of
what has already been written, so later files can call earlier ones correctly.
The original pattern matched a declaration keyword at the *start* of the line,
which works for Python/TS/Rust and drops almost everything in Swift, where
modifiers come first.

On spec_cc7dd609 the entire summary given to World.swift was four lines of
`struct X {` — not one initialiser, method or property — and six of its
cross-file calls were consequently wrong: `next()` for `next(max:)`,
`MushroomCell()` for a type with a stored property, and `==` on types never
declared Equatable.
"""
from coding_model_autonomous.executor import summarize_written_files

# Trimmed from the files spec_cc7dd609 retry 1 actually produced.
SEEDED_RNG = """\
/// A deterministic Linear Congruential Generator for reproducible randomness.
struct SeededRandomGenerator {
    private var state: UInt64

    /// Creates a new generator seeded from the provided integer.
    init(seed: Int) {
        self.state = UInt64(seed)
        if self.state == 0 {
            let fallback = 1
            self.state = UInt64(fallback)
        }
    }

    mutating func next(max: Int) -> Int {
        precondition(max > 0, "max must be greater than zero")
        let scaled = Int(state % UInt64(max))
        return scaled
    }
}
"""

HIT_RESULT = """\
public enum HitResult {
    case empty
    case mushroomDamaged(hitsAbsorbed: Int)
    case mushroomDestroyed
    case split(frontChainIndex: Int, rearChainIndex: Int)
}

extension HitResult {
    @discardableResult
    public static func describe(_ r: HitResult) -> String {
        switch r {
        case .empty: return "empty"
        case .mushroomDestroyed: return "destroyed"
        default: return "other"
        }
    }
}
"""


def _summary(name, src):
    return summarize_written_files([(name, src)])


# ── the declarations that were being lost ────────────────────────────────────

def test_initialiser_is_captured():
    assert "init(seed: Int)" in _summary("R.swift", SEEDED_RNG)


def test_mutating_method_is_captured():
    """The exact miss: the line starts with `mutating`, not `func`."""
    assert "mutating func next(max: Int) -> Int" in _summary("R.swift", SEEDED_RNG)


def test_stored_property_is_captured():
    """Stored properties define the memberwise initialiser callers must satisfy."""
    assert "private var state: UInt64" in _summary("R.swift", SEEDED_RNG)


def test_enum_cases_are_captured():
    out = _summary("H.swift", HIT_RESULT)
    for case in ("case empty", "case mushroomDamaged(hitsAbsorbed: Int)",
                 "case mushroomDestroyed", "case split(frontChainIndex: Int"):
        assert case in out, case


def test_extension_and_attributed_static_are_captured():
    out = _summary("H.swift", HIT_RESULT)
    assert "extension HitResult" in out
    assert "public static func describe" in out


def test_final_class_and_private_set_are_captured():
    src = ("final class World {\n"
           "    private(set) var chains: [Chain]\n"
           "    @MainActor func render() {}\n"
           "}\n")
    out = _summary("W.swift", src)
    assert "final class World" in out
    assert "private(set) var chains: [Chain]" in out
    assert "@MainActor func render()" in out


# ── things that must NOT be swept in ─────────────────────────────────────────

def test_switch_cases_are_not_mistaken_for_enum_cases():
    """`case .empty:` inside a switch is control flow, not interface."""
    out = _summary("H.swift", HIT_RESULT)
    assert 'case .empty: return "empty"' not in out
    assert "case .mushroomDestroyed" not in out
    assert "default: return" not in out


def test_local_bindings_are_not_captured():
    """A local `let x = 1` is not interface; a property annotation is."""
    out = _summary("R.swift", SEEDED_RNG)
    assert "let fallback = 1" not in out
    assert "let scaled = Int(" not in out


# ── the shapes that already worked must keep working ─────────────────────────

def test_python_declarations_still_captured():
    src = ("import os\n"
           "from typing import Optional\n\n"
           "class Thing:\n"
           "    def method(self, a: int) -> str:\n"
           "        x = 1\n"
           "        return str(x)\n")
    out = _summary("t.py", src)
    for want in ("import os", "from typing import Optional", "class Thing:",
                 "def method(self, a: int) -> str:"):
        assert want in out, want
    assert "x = 1" not in out


def test_typescript_declarations_still_captured():
    src = ("export interface Shape { kind: string }\n"
           "export function area(s: Shape): number {\n"
           "  const k = s.kind;\n"
           "  return 0;\n"
           "}\n")
    out = _summary("t.ts", src)
    assert "export interface Shape" in out
    assert "export function area(s: Shape): number" in out


def test_rust_declarations_still_captured():
    src = ("pub struct Grid { w: usize }\n"
           "impl Grid {\n"
           "    pub fn new(w: usize) -> Self { Self { w } }\n"
           "}\n")
    out = _summary("t.rs", src)
    assert "pub struct Grid" in out
    assert "impl Grid" in out


# ── framing and bounds ───────────────────────────────────────────────────────

def test_no_files_yet():
    assert "first file" in summarize_written_files([])


def test_file_with_no_declarations_says_so():
    assert "no exported symbols" in _summary("notes.md", "just prose\nmore prose\n")


def test_total_size_is_still_bounded():
    big = "\n".join(f"public func f{i}(a: Int) -> Int {{ return a }}" for i in range(400))
    out = summarize_written_files([(f"F{i}.swift", big) for i in range(20)],
                                  max_total_chars=2000)
    assert len(out) < 4000
    assert "further files omitted" in out


def test_per_file_line_cap_applies():
    src = "\n".join(f"public func f{i}() {{}}" for i in range(200))
    out = summarize_written_files([("F.swift", src)], max_sig_lines=5)
    assert out.count("public func") == 5


def test_the_regression_that_motivated_this():
    """World.swift must be able to see every contract it needs to satisfy."""
    out = summarize_written_files([
        ("Sources/CentipedeCore/SeededRandomGenerator.swift", SEEDED_RNG),
        ("Sources/CentipedeCore/HitResult.swift", HIT_RESULT),
    ])
    # The four cross-file mismatches from the real run, all now visible:
    assert "next(max: Int)" in out          # was called as next()
    assert "var state: UInt64" in out       # memberwise init shape
    assert "case mushroomDamaged" in out    # payload to pattern-match
    assert "init(seed: Int)" in out
