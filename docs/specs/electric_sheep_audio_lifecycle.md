# Handle audio interruptions, route changes, and scene phase; defer session activation

Jira: DEV-595 (high). Includes the related medium finding (session activated at launch).

## Context

Repo: `electric-sheep`. `Audioscape.swift` / `AudioManager.swift` own an `AVAudioEngine`
with an `AVAudioSourceNode` synth; `isPlaying` drives the ContentView Play/Stop button
and green "Playing" indicator. On iOS/visionOS an `AVAudioSession` is configured
`.playback` and activated at `Audioscape.swift:84-87`, all via `try?`. That code is in
`setupAudioGraph()`, which `Audioscape.init()` calls unconditionally; `AudioManager.init()`
constructs an `Audioscape`; and `ElectricSheepApp` holds
`@State private var audioManager = AudioManager()` (line 16). So the session is activated
during App-struct initialisation, at launch, before any user action.

`ElectricSheepApp` does NOT currently observe `scenePhase` — there is no
`@Environment(\.scenePhase)` in the file. Requirement 2 below ADDS that observation; it
is not a rewiring of something already present.

## Problem

1. No observer anywhere for `AVAudioSession.interruptionNotification`,
   `AVAudioSession.routeChangeNotification`, or
   `.AVAudioEngineConfigurationChange`. A Siri/call interruption (iOS/visionOS) or an
   output-route change (headphones unplugged — macOS engines get configuration-change
   too) stops the engine while `isPlaying` stays `true`: UI shows "Playing" over
   silence; the button reads "Stop"; the user must tap twice to recover.
2. Audio is never stopped/paused when the scene backgrounds.
3. Session activation at launch kills the user's Music/podcast playback on iOS/visionOS
   even if they never press Play; activation errors are swallowed by `try?`.

## Required change

1. Extract a small, platform-neutral, synchronous state machine (pure Swift, no AV
   imports) that owns the `isPlaying`/engine-intent reconciliation:
   inputs: userPressedPlay, userPressedStop, interruptionBegan,
   interruptionEnded(shouldResume:), configurationChanged, sceneBackgrounded,
   sceneForegrounded; outputs: startEngine / stopEngine / none + published isPlaying.
   Policy: interruption or config change while playing → stopEngine, isPlaying false
   (button truthfully shows "Play"); interruptionEnded(shouldResume: true) while the
   user had not pressed Stop → startEngine; scene background → stopEngine but remember
   user intent; foreground → resume only if user intent was playing.
2. Wire the state machine to the real notifications: `AVAudioSession` notifications
   under `#if os(iOS) || os(visionOS)`, `.AVAudioEngineConfigurationChange` on all
   platforms, and `scenePhase` in the App struct.
3. Move session configuration+activation out of `init()` into the play path (activate
   before `engine.start()`, deactivate with `.notifyOthersOnDeactivation` on stop).
   Replace `try?` with do/catch that reports through the existing state so the UI can
   show a failure instead of silently doing nothing.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit tests for the state machine covering at minimum: play → interruptionBegan →
  isPlaying == false and output stopEngine (FAILS on current main — no such component
  exists; say so); interruptionEnded(shouldResume: true) resumes only when the user
  never pressed Stop; configurationChanged while playing yields stop then (per policy)
  restart attempt; background/foreground round-trip preserves user intent; play-fail
  path surfaces an error state rather than isPlaying == true.
- Inspection criterion in the report: no `AVAudioSession` activation remains in any
  `init()` path, and no `try?` remains on session activate/deactivate or engine start.

## Change surface

Repo-relative paths, so context assembly can resolve them. Every modified path was
verified to exist at `main` before this spec was submitted.

| Path | Change |
| --- | --- |
| `ElectricSheep/AudioManager.swift` | modified |
| `ElectricSheep/Audioscape.swift` | modified |
| `ElectricSheep/ElectricSheepApp.swift` | modified |
| `ElectricSheep/AudioLifecycle.swift` | new |
| `ElectricSheepTests/AudioLifecycleTests.swift` | new |

Both new files go in those exact directories. The Xcode project uses synchronized
root groups for exactly `ElectricSheep/` and `ElectricSheepTests/`, so a source file
written anywhere else is never compiled and never joins a target — a test placed at
the repository root would leave the suite green while the new tests silently do not
exist.

`AudioLifecycle.swift` holds the pure state machine of requirement 1. It imports
Foundation and nothing else: no AVFoundation, no SwiftUI. That is what lets it be
tested on macOS with no audio hardware, no engine and no session.

## Reference files (read-only)

The test strategy protects three files. Read them, do not edit them.

- `ElectricSheep/ContentView.swift` — the real bindings the compatibility constraint
  is about. Check your changes against lines 219-232 rather than against memory.
- `ElectricSheep/Protocols.swift` — `AudioStrikeTarget` and its conformance.
- `ElectricSheepTests/BridgeLifecycleTests.swift` — house style for a new test file
  (`import XCTest`, `@testable import ElectricSheep`, `final class ...: XCTestCase`).
  Four of the five existing test files use swift-testing (`import Testing`) instead;
  either is accepted, but do not mix the two in one file.

Every path in the plan's phases should be one of the repo-relative paths above. A
bare filename is not shorthand; it is the defect class of DEV-601.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    skip_filter: ElectricSheepTests/DtypeContainmentTests
    protected_paths:
      - ElectricSheep/ContentView.swift
      - ElectricSheep/Protocols.swift
      - ElectricSheepTests/BridgeLifecycleTests.swift

## Constraints

- No new dependencies. State machine must compile and be tested on macOS (no
  AVAudioSession symbols in it).
- Do not touch synthesis or the render callback (covered by
  `electric_sheep_audio_strike_race.md`).
- Keep `AudioManager`'s public surface source-compatible; additive changes only. The
  real surface, read from `AudioManager.swift` at `main` before this spec was
  submitted, is:

      var isPlaying: Bool          var volume: Float
      func togglePlayback() throws func setVolume(_ value: Float)
      func triggerEvent(_ type: HallucinationType)
      func pushLevels(_ levels: [Float])
      func strike(_ metric: TokenMetrics)

  Note what is NOT on this type: there is no `updateIntensity` on `AudioManager`.
  That name belongs to `Audioscape` (`Audioscape.swift:50`), which `setVolume`
  calls through. `ContentView` binds to `isPlaying` (lines 219, 223, 225),
  `togglePlayback()` (line 220, as `try?`) and `volume`/`setVolume` (lines 231-232)
  — so `volume` and `setVolume` are load-bearing UI contract, not incidental.
- `strike(_:)` additionally satisfies `AudioStrikeTarget` (`Protocols.swift:10-12`,
  conformance at line 17). Changing its signature breaks `MetricsParticleBridge`.

## Risks

- visionOS session behavior differs from iOS in spatial contexts; the notification names
  are the same — verify availability against the SDK swiftinterface before writing
  (do not assume from memory).
- `AVAudioEngineConfigurationChange` fires on the main thread is NOT guaranteed —
  hop to MainActor before mutating observable state.
- Double-restart loops: a config change can fire again as a result of restarting the
  engine; the state machine must be idempotent (startEngine when already started = none).
