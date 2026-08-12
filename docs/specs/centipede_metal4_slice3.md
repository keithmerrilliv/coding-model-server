# Centipede — logic core, slice 3 (spider, flea, scorpion, poison)

## Context

This targets the **existing** `centipede` repository on the Mac runner (`~/Dev/Metal/Centipede`,
default branch `main`), continuing the multi-slice Metal 4 build. Slice 1 shipped the core
(chains, mushrooms, `hit(at:)`, determinism); slice 2 shipped the blaster and darts feeding that
entry point.

**PREREQUISITE:** slices 1 AND 2 must both be merged into `main` before this spec is submitted
— verify `Sources/CentipedeCore/World.swift` exists on `main` and contains the blaster/dart
state, or stop and merge first.

Extend the package. Do not restructure it, do not create an Xcode project, do not start a new
package. This slice adds the three remaining enemies and mushroom poisoning. Later slices add
scoring/lives/waves (slice 4) and the Metal 4 renderer (slice 5). Build only what this document
lists.

## Required behavior

**Common to all three enemies**
- Each enemy is a value type in `World`, at most one of each active at a time, with a
  construction seam: the explicit `World` initializer accepts optional `spider:`, `flea:`,
  `scorpion:` so tests place them exactly.
- Enemies move during `step()`, after chains (pin the full order in the design: darts → chains
  → spider → flea → scorpion).
- A dart reaching an enemy's cell removes that enemy and surfaces a new `HitResult` case
  (`.spider` / `.flea` / `.scorpion`, or a single `.enemy(kind:)` — design's choice, stated
  once). Slice 1/2's existing cases and their semantics are untouched.
- Automatic spawning is driven by the world's `SeededRNG` so seeded runs stay deterministic;
  every spawn decision consumes RNG state in a documented order. Tests use the construction
  seam and never depend on spawn timing.

**Spider**
- Moves one cell diagonally per tick within its band: rows
  `Field.rows - 10` through `Field.rows - 1` (20–29). Vertical direction reverses at the band
  edges; horizontal direction reverses at the field edges.
- Eats (removes) any mushroom in the cell it enters. Eating is unconditional and silent — no
  `HitResult`, no score in this slice.
- Leaves the field only by dart removal in this slice (despawn rules can be tightened in
  slice 4 with scoring).

**Flea**
- Descends one row per tick in a fixed column.
- Leaves a fresh mushroom (`damageLevel` 0, unpoisoned) in each cell it vacates.
- Despawns after leaving the bottom row.

**Scorpion**
- Crosses the field horizontally, one cell per tick, at its constructed row.
- Sets `poisoned = true` on any mushroom in the cell it enters. It never destroys mushrooms.
- Despawns after leaving either edge.

**Poison** — the behavioral centrepiece
- `Mushroom` gains `var poisoned: Bool = false`. The default preserves every existing
  construction call site (`Mushroom()`, `Mushroom(damageLevel:)`) and Equatable synthesis; the
  protected slice-1/2 tests must still compile and pass unchanged.
- When a chain's horizontal target holds a POISONED mushroom, the chain does not turn — it
  enters a diving state: it descends one row per tick, keeping every segment's column, until
  its head reaches the player zone's top row (`Field.rows - Field.playerZoneRows`), where it
  resumes normal horizontal movement. The ordinary blocked-turn rule (edge or unpoisoned
  mushroom) is unchanged.
- Diving state is per-chain, value-typed, and visible in `snapshotChains()` so tests can
  assert it.
- Damaging or destroying a poisoned mushroom via `hit(at:)` follows the ordinary 4-hit rule;
  poison does not change dart interactions.

## Architecture requirements

- Everything stays in `CentipedeCore`, headless-testable, structs only, internal access, no
  `public`, no new dependencies.
- Slice 1/2 member signatures unchanged; the protected test files are the mechanical contract.
- Every enemy movement rule above is exact — one cell per tick, the stated bounce/despawn
  edges — so tests are computable without running the RNG.
- New tests in a NEW file (e.g. `EnemyTests.swift`); slice 1/2 test files are read-only.

## Out of scope — do not implement

- Scoring for enemy kills, lives, life loss, blaster-enemy collision, game-over, restart,
  waves (slice 4).
- Enemy spawn tuning, difficulty curves, per-wave behavior changes (slice 4+).
- Any renderer or input work (slice 5 and callers).

## Constraints

Identical to slice 2: Swift only, existing SwiftPM layout, no new dependencies, no
`Package.swift` changes, latest non-beta toolchain, Swift Testing framework.

## Acceptance criteria

`swift test` passes with no failures — including every slice-1 and slice-2 test unchanged —
and coverage includes at minimum:

- A seam-constructed spider moves one diagonal cell per tick and reverses vertical direction
  at rows 20 and 29 and horizontal direction at columns 0 and 29.
- A spider entering a mushroom's cell removes that mushroom.
- A flea descends one row per tick and leaves a fresh mushroom in each vacated cell; after
  leaving row 29 it is gone from the snapshot.
- A scorpion entering a mushroom's cell sets `poisoned` without changing `damageLevel`; after
  leaving the field it is gone from the snapshot.
- A chain whose horizontal target holds a poisoned mushroom begins diving: next tick every
  segment is one row lower, columns unchanged, and the snapshot shows the diving state.
- A diving chain resumes horizontal movement at the player-zone boundary row and stops diving.
- A chain whose target holds an UNPOISONED mushroom still turns per slice 1 (regression pin).
- A dart reaching each enemy kind removes it and surfaces the new `HitResult` case; existing
  cases are untouched (construct one world per kind).
- Two identically-seeded worlds with identical call sequences produce identical snapshots with
  enemies active (extends slice 1's determinism criterion over the new state).

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Sources/CentipedeCore/GameState.swift
      - Tests/CentipedeCoreTests/GameStateTests.swift
      - Tests/CentipedeCoreTests/WorldTests.swift
      - Tests/CentipedeCoreTests/BlasterTests.swift
      - Package.swift
    notes: |
      Runs on the Mac runner via `swift test`, headless, sandbox-exec confined. Slice 1's and
      slice 2's test files are protected as the regression contract; sources are editable
      because Swift extensions cannot add stored properties and the enemies live in World.

## Risks

- Poison-dive interacting with the blocked-turn rule is the subtle part: the design must state
  the precedence (poisoned target → dive; unpoisoned target or edge → turn) in one place and
  test both on adjacent cells.
- The step order now has five phases; an implementer that reorders them breaks determinism
  invisibly. Pin the order in the design's invariants AND in a criterion seam.
- Adding a `HitResult` case is source-compatible but a design that RENAMES existing cases
  breaks the protected tests — the build check catches it; read that failure as a slice-3
  defect, not a test problem.
- Mushroom's memberwise init changes shape when `poisoned` is added; keeping a default value
  for BOTH fields preserves every existing call site. The design must show the exact
  declaration.
