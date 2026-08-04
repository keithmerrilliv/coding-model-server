# Centipede — logic core, slice 1 (mushroom field, centipede movement, split-on-hit)

## Context

This targets an **existing** repository, not a greenfield project. The Mac runner has it
registered as `centipede` (`~/Dev/Metal/Centipede`, default branch `main`), and it already
contains a working SwiftPM scaffold with a green test baseline:

- `Package.swift` — swift-tools 6.0, `platforms: [.macOS(.v15)]`
- `Sources/CentipedeCore/GameState.swift` — seed types `GameState` (score/lives/wave) and
  `Field` (30×30 grid constants)
- `Tests/CentipedeCoreTests/GameStateTests.swift` — 2 passing tests using **Swift Testing**
  (`import Testing`, `@Test`, `#expect`), not XCTest

Extend that package. Do not restructure it, do not create an Xcode project, and do not
start a new package.

This is **slice 1 of a multi-slice build** of the classic 1981 Atari arcade game Centipede.
It covers the deterministic simulation of the mushroom field, centipede locomotion, and the
split behaviour that defines the game. Later slices add the remaining enemies, scoring, and
the renderer. Build only what this document lists.

## Required behavior

**Mushroom field**
- The field is a 30-column × 30-row grid of cells, addressed by column and row, with row 0
  at the top.
- Mushrooms are seeded pseudo-randomly at world creation from an explicit integer seed. The
  same seed must always produce the identical field.
- No mushrooms are seeded in the bottom 5 rows (the player zone) at world creation.
- A mushroom absorbs exactly 4 hits: hits 1–3 leave it standing with increasing damage
  (0…3), the 4th removes it from the field.

**Centipede**
- A centipede is an ordered chain of segments — one head followed by body segments. The
  world starts with a single chain of 12 segments entering from the top row.
- On each tick the chain advances one cell in its current horizontal direction, each segment
  taking the cell the segment ahead of it occupied.
- When the head would move into a field edge or a mushroom, the chain instead descends one
  row and reverses horizontal direction.
- A chain that descends past the last row is removed from the world.

**Hits and splitting** — the centrepiece of this slice

There is no projectile in this slice. Hits arrive through a single explicit entry point on
the world that resolves a strike against one cell. Slice 2's dart will call into that same
entry point when it collides, so design it as the collision-resolution primitive of the
simulation, not as a test-only hook.

- A hit on a cell holding a mushroom damages it, per the 4-hit rule above.
- A hit on a cell holding a **body** segment removes that segment, leaves a mushroom in the
  cell it occupied, and splits its chain into two independent chains: the portion ahead of
  the hit keeps its head and direction, and the portion behind the hit becomes a new chain
  whose frontmost segment — the one that was nearest the struck cell — is promoted to head.
- **The trailing chain keeps the parent's direction and segment order.** Do not reverse it
  at split time, and do not reverse its array. It is left pointing at the mushroom that the
  hit just created, so on its next step the ordinary turn-on-collision rule blocks it, and
  it descends one row and reverses. That descent-and-reversal is the correct behaviour and
  it must *emerge* from the existing rule — a special-case reversal inside the split would
  both skip the descent and double up with the collision rule a tick later.
- A split that would leave an empty portion produces no empty chain.
- A hit on a **head** segment removes it, leaves a mushroom in that cell, and promotes the
  next segment in the chain to head; the chain keeps its direction. A chain whose last
  segment is destroyed is removed from the world.
- A hit on an empty cell changes nothing.
- What the hit struck must be observable to the caller — slice 2 needs that outcome, and the
  tests assert on it.

**Simulation**
- A single `step()` advances the world one tick: every centipede chain moves once, per the
  locomotion rules above. Hits are applied by the caller between steps, never from inside
  `step()`.
- Given the same seed and the same sequence of calls, the resulting world states must be
  identical every run.

## Architecture requirements

- All of it lands in the `CentipedeCore` target and stays free of rendering, windowing, and
  input frameworks — no `import Metal`, `MetalKit`, `AppKit`, `SwiftUI`, or `Foundation`
  APIs that touch the display. The runner tests this target headlessly with no GPU or
  window server.
- Randomness comes from a small seeded generator owned by the world, not from
  `Int.random(in:)`, `arc4random`, or `SystemRandomNumberGenerator` — those defeat the
  determinism requirement.
- World state is inspectable enough to be asserted against: tests must be able to read
  mushroom presence and damage at a cell, and the segments and direction of each chain.
- The existing `Field` constants and the 2 existing tests must keep working. `GameState` may
  be extended if useful, but nothing in this slice requires score, lives, or wave changes.

## Out of scope — do not implement

These belong to later slices. Implementing them here is a scope violation, not a bonus:

- **Any renderer.** No Metal, no Metal 4, no shaders, no windowing, no app target, no
  `.xcodeproj`. The Metal 4 renderer is a deliberate later slice and cannot be verified by
  the runner.
- Spider, flea, and scorpion; mushroom poisoning and the resulting dive behaviour.
- Scoring, lives, life loss, game-over, restart, and wave advancement (including the
  speed increase between waves).
- The blaster and darts: player position, movement, firing, projectile travel, and the
  one-dart-at-a-time rule. Slice 2 adds these on top of the hit entry point above — this
  slice must not anticipate them with a player entity or a dart type.
- Player input handling, key bindings, and any real-time or frame-paced game loop. `step()`
  is called by tests, not by a display link or timer.
- Sound, persistence, and configuration files.

## Constraints

- Swift only, using the existing SwiftPM package layout. No new dependencies — the package
  must stay dependency-free so `swift test` needs no network.
- Latest non-beta Swift toolchain and macOS SDK. No betas, previews, or experimental flags.
- Tests use **Swift Testing** (`import Testing`, `@Test`, `#expect`), matching the existing
  test file. Do not introduce XCTest.
- Do not modify `Package.swift`'s tools version or platforms, and do not add targets.

## Acceptance criteria

`swift test` passes with no failures, and coverage includes at minimum:

- The same seed produces an identical mushroom field; two different seeds produce different
  fields.
- No mushrooms are seeded in the bottom 5 rows.
- A mushroom survives 3 hits with increasing damage and is removed by the 4th.
- A chain advances one cell per tick with each segment following the one ahead.
- A chain reaching the field edge descends one row and reverses direction.
- A chain blocked by a mushroom descends one row and reverses direction.
- In both blocked cases the head **keeps its own column** — the blocked horizontal step is
  replaced by the descent, not combined with it. The head never enters the cell that
  blocked it, and never leaves the field's horizontal bounds.
- A hit on the head leaves a mushroom in the cell the head vacated, exactly as a body hit
  does.
- A chain descending past the last row is removed.
- A hit on a middle body segment yields exactly two chains, with the correct segment counts
  either side, a new head on the trailing chain, and a mushroom left in the struck cell.
- Immediately after that split the trailing chain still holds the parent's direction and
  segment order — it is not reversed at split time.
- Stepping once more, the trailing chain is blocked by the mushroom the hit created, and so
  descends one row and reverses via the ordinary turn-on-collision rule.
- A hit on the segment directly behind the head yields two chains, one of which is the head
  alone.
- A hit on the head promotes the next segment and leaves the chain count unchanged.
- A hit on the only remaining segment of a chain removes that chain from the world.
- A hit on an empty cell leaves the world unchanged.
- A full run of N ticks from a fixed seed, with a fixed sequence of hits applied between
  steps, reproduces an identical world state across two runs.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Sources/CentipedeCore/GameState.swift
      - Tests/CentipedeCoreTests/GameStateTests.swift
      - Package.swift
    notes: |
      Runs on the Mac runner via `swift test` against the registered `centipede` repo.
      The suite must pass headlessly: no GPU, no window server, no network. The runner
      executes it confined by sandbox-exec, so anything reaching outside the worktree and
      its build directories will fail. Keep every test in CentipedeCore's test target;
      there is no app target and no xcodebuild path in this slice.

      `protected_paths` names the files this spec puts off-limits. They are never sent to
      the runner, so the worktree keeps the `main` version of each — write to them and
      your version is discarded, not merged. `Package.swift` is on that list because the
      Constraints section forbids changing its tools version, platforms or target list,
      and prose alone does not enforce anything: only this key does.

## Risks

- Split-on-hit is the subtle part: two new chains, correct segment ownership on each side,
  head promotion on the trailing chain, and direction preserved. Off-by-one errors in
  segment ownership are the most likely defect and are why the acceptance list pins the
  middle-segment, behind-the-head, head, and last-segment cases separately.
- The trailing chain's reversal is settled and is **emergent, not special-cased** — see the
  behaviour section. A reviewer has already argued once for reversing it at split time;
  that is wrong here, because it skips the descent and then collides with the ordinary
  turn-on-collision rule on the following tick. Do not reopen it.
- Turn-on-collision has an ordering trap: whether the descent happens before or after the
  reversal changes which cell the head lands in. Pick one order, state it, and test it.
- Determinism breaks silently. A single use of a system RNG or of `Set`/`Dictionary`
  iteration order in logic that feeds world state will pass tests intermittently. Prefer
  ordered collections anywhere iteration order can influence the result.
- The existing scaffold's tests are the regression canary for "the patch actually landed in
  the right place" (DEV-399). If they stop running, suspect file placement before logic.
