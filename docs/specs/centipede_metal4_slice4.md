# Centipede — logic core, slice 4 (scoring, lives, waves, game over)

## Context

This targets the **existing** `centipede` repository on the Mac runner (`~/Dev/Metal/Centipede`,
default branch `main`), continuing the multi-slice Metal 4 build. Slices 1–3 shipped the core:
chains and mushrooms, blaster and darts through `hit(at:)`, and the three enemies with poison.

**PREREQUISITE:** slices 1–3 must all be merged into `main` before this spec is submitted.

Extend the package. Do not restructure it, do not create an Xcode project, do not start a new
package. This slice completes the game's rules: scoring, lives, wave progression, game over,
restart. Slice 5 (the Metal 4 renderer) is the only remaining slice. Build only what this
document lists.

## Required behavior

**Scoring** — attach points to events that already exist
- The scaffold's `GameState` already carries `score`, `lives`, `wave` (protected file — the
  design wires them, it does not redeclare them; if the shipped `GameState` shape cannot be
  written to from `World`, the design states where score/lives/wave live instead, ONCE).
- Point values (classic): mushroom hit 1 per damaging hit; body segment 10; head 100;
  spider 300; flea 200; scorpion 1000. Every point award flows from the `HitResult` the
  existing entry point already surfaces — scoring is a consumer of slice 1–3's events, not a
  new resolution path.
- Score accumulates monotonically within a game and is visible in a snapshot accessor.

**Lives and game over**
- The world starts with 3 lives.
- The blaster is hit when ANY enemy or chain segment enters (or is constructed in) the
  blaster's cell, checked at the end of each `step()`: lives decrease by one, every enemy and
  dart is cleared, chains persist, the blaster returns to its start cell (15, 28).
- At 0 lives the world is game-over: `step()` becomes a no-op, `hit(at:)` and fire calls
  change nothing, and the state is visible in a snapshot accessor.

**Waves**
- When the last chain segment is destroyed (chains empty after a hit or step), the wave
  advances: `wave += 1`, a new 12-segment chain enters at row 0 from the left edge moving
  right, and the wave's speed level increases by one.
- Speed level is DATA in this slice: a stored, snapshot-visible `speedLevel` (wave 1 → 1,
  incrementing per wave). Consuming it for actual movement pacing is the renderer/loop's job
  in slice 5 — core movement stays one cell per tick.

**Restart**
- A restart call re-seeds the world from an explicit seed: fresh field per slice 1's seeding,
  3 lives, score 0, wave 1, speed level 1, blaster at start, no enemies, no darts.
- Restart from game-over works; restart mid-game works; determinism holds (restart with seed S
  equals a fresh `World(seed: S)` in every snapshot).

## Architecture requirements

- Everything stays in `CentipedeCore`, headless, structs, internal access, no new dependencies.
- Slice 1–3 member signatures unchanged; protected test files are the contract.
- Scoring must not fork resolution: one code path resolves strikes (slice 1's), and scoring
  observes its result. A design with a second "and also award points here" resolution branch
  is wrong.
- New tests in a NEW file (e.g. `GameRulesTests.swift`); earlier test files read-only.

## Out of scope — do not implement

- Any renderer, HUD, input handling, or timing (slice 5).
- High-score persistence, sound, configuration (out of the project's scope entirely).
- Difficulty tuning beyond the stated wave speed-level increment.

## Constraints

Identical to slices 2–3: Swift only, existing SwiftPM layout, no new dependencies, no
`Package.swift` changes, latest non-beta toolchain, Swift Testing framework.

## Acceptance criteria

`swift test` passes with no failures — including every slice-1/2/3 test unchanged — and
coverage includes at minimum:

- Each `HitResult` kind awards exactly its stated points (six worlds, one per kind, each
  asserting the exact score delta).
- Destroying a mushroom over four hits scores 1 per damaging hit (total 4).
- A spider constructed in the blaster's cell costs one life at the next step; enemies and
  darts clear; chains persist; the blaster is back at (15, 28).
- Three losses reach game-over: subsequent `step()` and fire calls leave every snapshot
  identical.
- Destroying the last segment advances the wave: wave increments, a fresh 12-segment chain
  appears at row 0, speed level increments.
- Restart mid-game with seed S produces snapshots equal to a fresh `World(seed: S)`.
- Restart from game-over restores a playable world (a step changes state again).
- Determinism: two identically-seeded worlds given an identical mixed sequence (moves, fires,
  steps through a life loss and a wave advance) end with identical snapshots including score,
  lives, wave, speed level.

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
      - Tests/CentipedeCoreTests/EnemyTests.swift
      - Package.swift
    notes: |
      Runs on the Mac runner via `swift test`, headless, sandbox-exec confined. All prior
      slices' test files are protected as the regression contract. GameState.swift remains
      protected scaffold: the design must WIRE its fields or place game-rule state in World,
      stated once — it must not redeclare GameState.

## Risks

- The blaster-collision check happening at end-of-step interacts with slice 3's five-phase
  order: an enemy moving INTO the blaster cell during phases 3–5 must register that tick. Pin
  it as phase 6 and test with a spider constructed one diagonal cell away.
- Game-over as "step is a no-op" is easy to implement as "step throws" or "step keeps moving
  chains" — the acceptance pins snapshot identity, test it literally.
- Wave advance triggered by a HIT (not only by a step) is the corner: the last segment dying
  to `hit(at:)` mid-tick must still advance the wave exactly once.
- GameState.swift is protected but is also where score/lives/wave nominally live — if the
  scaffold's shape blocks writing, the correct move is a design that states the alternative
  home ONCE, not a rewrite attempt on a protected file (the runner discards it).
