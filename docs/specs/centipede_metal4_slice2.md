# Centipede — logic core, slice 2 (blaster and darts)

## Context

This targets the **existing** `centipede` repository on the Mac runner (`~/Dev/Metal/Centipede`,
default branch `main`), continuing the multi-slice Metal 4 build. Slice 1 (run 12,
spec_f66a31de) shipped the logic core: `Position`, `Mushroom`, `CentipedeChain`, `HitResult`,
`SeededRNG`, and `World` in `Sources/CentipedeCore/`, with 18 criterion tests in
`Tests/CentipedeCoreTests/WorldTests.swift` — all green.

**PREREQUISITE — verify before submitting this spec:** run 12's output must be merged into the
repo's `main`. As of 2026-08-11 the pipeline had landed it as a patch only;
`Sources/CentipedeCore/World.swift` did not yet exist on `main`. If `read_files` returns empty
for that path, stop and merge first.

Extend the package. Do not restructure it, do not create an Xcode project, and do not start a
new package.

This slice adds the player's half of the collision loop: the blaster and its darts, feeding the
`hit(at:)` entry point slice 1 built exactly for this purpose. Later slices add the remaining
enemies (slice 3), scoring/lives/waves (slice 4), and the Metal 4 renderer (slice 5). Build only
what this document lists.

## Required behavior

**Blaster**
- The blaster occupies one cell, confined to the player zone: rows
  `Field.rows - Field.playerZoneRows` through `Field.rows - 1` (25–29), columns 0–29.
- A world constructed via the seam can place the blaster at any chosen in-zone cell; the seeded
  world starts it at column 15, row 28.
- The blaster moves one cell per explicit call (`moveBlaster(dx:dy:)` or equivalent), clamped to
  the zone — a move that would leave the zone leaves the blaster where it is on that axis.
- The blaster is not an obstacle to chains in this slice; interactions with enemies and life
  loss are slice 4's concern.

**Darts** — the collision loop's projectile half
- At most ONE dart is in flight. A fire call while a dart is in flight does nothing.
- Firing spawns a dart one row above the blaster's cell (if the blaster is at row r, the dart
  starts at row r-1, same column).
- Each `step()` the dart moves up exactly one row BEFORE the chains move (pin this order; it is
  what makes dart behavior deterministic relative to slice 1's movement).
- When the dart's destination cell is occupied by a mushroom or a chain segment, the strike is
  resolved by calling slice 1's `hit(at:)` — **the same entry point, not a reimplementation** —
  and the dart is removed. The `HitResult` from that call must surface to the caller of
  `step()` (e.g. `step()` returns it, or a `lastHit` accessor exposes it) so slice 4 can score
  it and tests can assert on it.
- A dart whose destination row would be < 0 is removed with no effect.
- Resolution happens at the dart's destination cell only — no swap/pass-through detection in
  this slice.

**Simulation**
- `step()` keeps its slice-1 signature and semantics for chains; darts are handled inside it,
  before chain movement, per the order above.
- Given the same seed and the same sequence of calls (moves, fires, steps, hits), world states
  are identical every run.

## Architecture requirements

- Everything stays in `CentipedeCore`, free of rendering/input frameworks, headless-testable.
- New state (blaster, dart) lives in `World` as value types (`struct Blaster`, `struct Dart`,
  both Equatable), following slice 1's idiom: structs only, internal access, no `public`,
  headness/order conventions unchanged.
- **Slice 1's existing members and method signatures must not change.** The protected slice-1
  test file enforces this mechanically — if `WorldTests.swift` stops compiling or passing, the
  slice-2 change is wrong, not the tests.
- Snapshot accessors for the new state: `snapshotBlaster() -> Blaster` and
  `snapshotDarts() -> [Dart]`, mirroring slice 1's snapshot style.
- No new uses of the RNG — this slice is fully deterministic given calls.

## Out of scope — do not implement

- Spider, flea, scorpion; mushroom poisoning and dive behavior (slice 3).
- Scoring, lives, life loss, game-over, restart, wave advancement (slice 4).
- Any renderer, input handling, key bindings, or real-time loop (slice 5 and callers).
- Multi-dart modes, dart cooldowns, or autofire — the one-dart rule is the rule.

## Constraints

- Swift only, existing SwiftPM layout, no new dependencies, no `Package.swift` changes.
- Latest non-beta toolchain. Tests use **Swift Testing** (`import Testing`, `@Test`,
  `#expect`) in `Tests/CentipedeCoreTests/` — a NEW file (e.g. `BlasterTests.swift`); the
  slice-1 test files are read-only.

## Acceptance criteria

`swift test` passes with no failures — including every slice-1 test unchanged — and coverage
includes at minimum:

- A seam-constructed blaster at a chosen in-zone cell; a seeded world's blaster at (15, 28).
- Each of the four moves works one cell; moves that would exit the zone clamp (both axes,
  all four edges).
- Firing with no dart in flight spawns a dart one row above the blaster, same column.
- Firing while a dart is in flight changes nothing (dart count still 1, position unchanged).
- A dart advances exactly one row per step, before chains move.
- A dart reaching a mushroom cell damages it via the 4-hit rule, surfaces
  `.mushroom(damageAfter:)`, and is removed.
- A dart reaching a body segment splits the chain exactly as slice 1's rules dictate (two
  chains, mushroom in the struck cell, trailing order preserved) and surfaces `.body(...)` —
  proving the strike went through `hit(at:)`.
- A dart reaching a head cell surfaces `.head(...)` with slice-1 promotion semantics.
- A dart leaving the top of the field is removed with no world change beyond its removal.
- Two identically-seeded worlds given the same move/fire/step sequence produce identical
  snapshots (mushrooms, chains, blaster, darts).

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Sources/CentipedeCore/GameState.swift
      - Tests/CentipedeCoreTests/GameStateTests.swift
      - Tests/CentipedeCoreTests/WorldTests.swift
      - Package.swift
    notes: |
      Runs on the Mac runner via `swift test`, headless, sandbox-exec confined. The slice-1
      test file WorldTests.swift is protected ON PURPOSE: it is the regression contract that
      slice 2 did not break the core. World.swift and the other slice-1 sources are
      deliberately NOT protected — Swift cannot add stored properties in extensions, so the
      implementer must edit World.swift to add blaster/dart state; the protected tests are
      what keeps those edits honest.

## Risks

- The dart/chain ordering inside `step()` is the subtle part — pin it in the design (darts
  resolve first) and test it with a chain adjacent to a dart's path.
- The temptation to reimplement strike resolution instead of calling `hit(at:)` is the exact
  failure this slice exists to avoid; the acceptance criteria assert on `HitResult` surfacing
  specifically to force reuse.
- Editing `World.swift` while its behavior is pinned by protected tests: any signature change
  breaks compilation of a read-only file, which the build check catches immediately — read the
  failure as "slice 2 changed something it must not" rather than attempting to regenerate the
  protected file (the runner discards such writes).
- Design-gate reviewers: hold the design to slice 1's standards — construction seams for every
  criterion, exact signatures, no tuples in Equatable positions, one declaration per type,
  conclusions not deliberation.
