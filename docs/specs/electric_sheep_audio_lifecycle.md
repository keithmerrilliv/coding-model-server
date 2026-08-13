# Handle audio interruptions, route changes, and scene phase; defer session activation

Jira: DEV-595 (high). Includes the related medium finding (session activated at launch).

## Context

Repo: `electric-sheep`. `Audioscape.swift` / `AudioManager.swift` own an `AVAudioEngine`
with an `AVAudioSourceNode` synth; `isPlaying` drives the ContentView Play/Stop button
and green "Playing" indicator. On iOS/visionOS an `AVAudioSession` is configured
`.playback` and activated in `init()` (Audioscape.swift lines ~84-87, all via `try?`),
which runs at app launch because `AudioManager` is `@State` on the App struct.

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

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies. State machine must compile and be tested on macOS (no
  AVAudioSession symbols in it).
- Do not touch synthesis or the render callback (covered by
  `electric_sheep_audio_strike_race.md`).
- Keep the public AudioManager API used by ContentView (`togglePlayback`, `isPlaying`,
  `updateIntensity`, `strike`, `pushLevels`) source-compatible; additive changes only.

## Risks

- visionOS session behavior differs from iOS in spatial contexts; the notification names
  are the same — verify availability against the SDK swiftinterface before writing
  (do not assume from memory).
- `AVAudioEngineConfigurationChange` fires on the main thread is NOT guaranteed —
  hop to MainActor before mutating observable state.
- Double-restart loops: a config change can fire again as a result of restarting the
  engine; the state machine must be idempotent (startEngine when already started = none).
