# Centipede — slice 5 (Metal 4 renderer)

## Context

This is the final slice of the Metal 4 Centipede build, targeting the **existing** `centipede`
repository on the Mac runner. Slices 1–4 shipped the complete, headless, deterministic game in
`CentipedeCore`. This slice draws it.

**PREREQUISITES — all three, verified before submitting:**
1. Slices 1–4 merged into `main`.
2. **A committed app scaffold.** The runner only materialises worktrees and applies patches —
   nothing on the pipeline side can create an `.xcodeproj`/app target (DEV-102). A human must
   scaffold the app shell (Xcode project or SwiftPM executable target with `MetalKit` window
   bootstrap), commit it to `main`, and extend `repos.yml` if the build invocation changes.
   Decide the shape BEFORE writing the design: a SwiftPM `executableTarget` keeps the
   whole build on `swift build` and avoids `xcodebuild` entirely — strongly preferred.
3. The app target must not break `swift test` for `CentipedeCore` (DEV-403: app-hosted test
   bundles cannot run under the runner's sandbox — keep ALL tests in the library target).

## Required behavior

- A macOS app that opens a window and renders the full play field via **Metal 4**: MTL4
  command structures, render command encoder, shaders in Metal Shading Language. No
  SpriteKit/SceneKit/Core Graphics for the play field.
- The renderer consumes `CentipedeCore` snapshots only — no game logic in the app target, no
  reaching into non-snapshot state.
- A fixed-timestep loop (CVDisplayLink/CADisplayLink driving an accumulator) calls `step()` at
  a fixed rate scaled by the core's `speedLevel`; rendering is decoupled from stepping.
- Keyboard input: arrows/WASD move the blaster via the slice-2 API; space fires. Input maps to
  core calls only.
- Renders: mushrooms with visible damage states (and poisoned tint), chains with heads
  distinguishable, spider/flea/scorpion, blaster, dart, score/lives/wave text (Metal-drawn or
  a plain overlay view — the play FIELD is what must be Metal).
- Game over shows a restart affordance wired to the slice-4 restart call.

## Architecture requirements

- Latest non-beta macOS SDK, Xcode, and Swift toolchain. No betas, no experimental flags.
- `CentipedeCore` remains dependency-free and unmodified in behavior; the app target may
  depend on it, never the reverse.
- All Metal state (device, queue, pipelines, buffers) lives in the app target.

## Out of scope

- Sound, persistence, configuration, menus beyond restart.
- Any change to core game rules or to the test suite's coverage of them.
- Performance work beyond a sane 60fps target on Apple Silicon.

## Acceptance criteria

- `swift test` (all slice 1–4 tests) passes unchanged — the CI gate for this slice is "the
  renderer broke nothing".
- The app builds on the runner (`swift build` of the executable target, or the agreed
  invocation from prerequisite 2).
- Manual acceptance by the human reviewer, on hardware: the game is playable start → life
  loss → wave advance → game over → restart, and every entity listed above is visible and
  correct. Record the verdict on the release gate.

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
      - Tests/CentipedeCoreTests/GameRulesTests.swift
      - Package.swift
    notes: |
      The automated gate covers the library tests and the build only; the renderer itself is
      exercised manually (headless runner: no GPU surface, no window server; DEV-403 forbids
      app-hosted test bundles under sandbox-exec). Protecting Package.swift here assumes the
      scaffold decision (prerequisite 2) landed the app target in it already — if the human
      scaffold used a separate manifest arrangement, update this list to match before
      submitting.

## Risks

- This slice's automated signal is intentionally thin: a compiling renderer with green core
  tests can still draw garbage. Budget human time at the release gate accordingly.
- Metal 4 API surface via a code model: expect shader/pipeline boilerplate defects that no
  test catches — the build check catches compilation only. Manual review of the WGSL-analog
  (MSL) files at the code-review gate is worth the minutes.
- The display-link loop must not call `step()` from a non-main thread while input mutates the
  world — pin the threading model in the design (single-threaded core access on the main
  thread is fine and simplest).
