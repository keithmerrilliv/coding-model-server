# Electric Sheep: a late download-progress update cannot undo a finished load

Jira: DEV-824, epic DEV-443. Repo `electric-sheep`, `main` = `116285d` (run 61's
delivery, DEV-815). Every fact below was read from that commit. A reference
implementation of exactly the edits below, with the tests described, ran on the Mac runner
before this spec was written: 112 passed, 0 failed, 1 skipped (the opt-in
`LiveGenerationTests`). The new test file alone, against unmodified `main`, fails to
compile (exit 65, 2 errors: "has no member" on `progressState` and on
`applyDownloadProgress`). The reference also builds for testing on the visionOS Simulator
destination. Do not re-derive the expected values.

## Context

`ElectricSheep/HallucinationEngine.swift` loads a model like this:

```swift
    func loadModel(_ model: AvailableModel) async {
        state = .downloading(progress: 0)

        do {
            let config = ModelConfiguration(id: model.rawValue)
            let container = try await LLMModelFactory.shared.loadContainer(
                configuration: config
            ) { progress in
                Task { @MainActor in
                    self.state = .downloading(progress: progress.fractionCompleted)
                }
            }
            self.modelContainer = container
            state = .ready
```

Every progress callback becomes an unstructured main-actor task. Nothing orders those
tasks against the `state = .ready` after the `await`, or against each other.

**The defect.** A progress task that runs after `state = .ready` puts the engine back
into `.downloading`. `canGenerate` is false while downloading, so Generate stays disabled
with the model loaded, and only a reload recovers. Separately, an update that runs after a
later one moves the progress bar backwards.

**The fix.** A pure function decides what one progress update does to the current state:
outside `.downloading` it changes nothing, and inside it the fraction only moves forward.
The engine applies it through one method, and the callback calls that method instead of
assigning `state` directly. The callback reads `fractionCompleted` before creating the
task, so the task captures a `Double`, not the non-Sendable `Progress`.

**Isolation.** The app target sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`.
`HallucinationEngine` is explicitly `@MainActor`, and its new members need no annotation
of their own: not `nonisolated`, not `@MainActor`. `ModelState` is already `Equatable`,
which the tests rely on.

## Required change

### `ElectricSheep/HallucinationEngine.swift` (modified — two anchored edits)

**Edit 1.** Replace these five lines inside `loadModel(_:)`

```swift
            ) { progress in
                Task { @MainActor in
                    self.state = .downloading(progress: progress.fractionCompleted)
                }
            }
```

with

```swift
            ) { progress in
                let fraction = progress.fractionCompleted
                Task { @MainActor in
                    self.applyDownloadProgress(fraction)
                }
            }
```

**Edit 2.** Immediately before the line `    func loadModel(_ model: AvailableModel) async {`,
add

```swift
    /// What one download-progress update does to `current` (DEV-824). The updates
    /// arrive as unordered main-actor tasks, so one can land after the load has
    /// finished, or after a later update. Outside `.downloading` it changes
    /// nothing; inside it, the bar only moves forward.
    static func progressState(current: ModelState, fraction: Double) -> ModelState {
        guard case .downloading(let shown) = current else { return current }
        return .downloading(progress: Swift.max(shown, fraction))
    }

    /// Applies one download-progress update through `progressState(current:fraction:)`.
    func applyDownloadProgress(_ fraction: Double) {
        state = Self.progressState(current: state, fraction: fraction)
    }

```

Both are internal, not private, because the tests call them. Call the static function
as `Self.progressState(...)` inside the class. Nothing else in the file changes. In
particular, the `state = .downloading(progress: 0)` at the top of `loadModel`, the
`state = .ready` and the `catch` branch stay exactly as they are.

### `ElectricSheepTests/LoadProgressTests.swift` (new)

XCTest: `import Foundation`, `import XCTest`, `@testable import ElectricSheep`, and
`final class LoadProgressTests: XCTestCase`. No `import MLX` is needed. Every test method
is `@MainActor func test_...() async throws`. Declare these private members inside the
class, verbatim:

```swift
    private typealias State = HallucinationEngine.ModelState

    @MainActor private func next(_ current: State, _ fraction: Double) -> State {
        HallucinationEngine.progressState(current: current, fraction: fraction)
    }
```

Compare states with `XCTAssertEqual`. `ModelState` is `Equatable`, and every fraction
below is exactly representable, so exact equality is correct. Every test builds all of
its state inside the test method; no test relies on another.

## Change surface

`ElectricSheep/HallucinationEngine.swift` was verified to exist at `main`, and
`ElectricSheepTests/LoadProgressTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these two as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/HallucinationEngine.swift` | modified (two anchored edits) |
| `ElectricSheepTests/LoadProgressTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. Every criterion uses
`progressState(current:fraction:)` or `applyDownloadProgress(_:)`, neither of which
exists on `main`, so on `main` the test file does not compile. The report must say so.

1. **A late update cannot undo `.ready`.** `next(.ready, 1.0)` and `next(.ready, 0.4)`
   both equal `.ready`.
2. **No other settled state changes.** `next(.idle, 0.5) == .idle`,
   `next(.loading, 0.5) == .loading`, `next(.generating, 0.5) == .generating`, and
   `next(.error("boom"), 0.5) == .error("boom")`.
3. **Downloading moves forward.** `next(.downloading(progress: 0.2), 0.5)` equals
   `.downloading(progress: 0.5)`, and `next(.downloading(progress: 0), 1.0)` equals
   `.downloading(progress: 1.0)`.
4. **Downloading never moves backwards.** `next(.downloading(progress: 0.8), 0.6)` and
   `next(.downloading(progress: 0.8), 0.8)` both equal `.downloading(progress: 0.8)`.
5. **An out-of-order run ends at its largest update, and `.ready` holds.** One test:
   start from `var state: State = .downloading(progress: 0)`, apply
   `state = next(state, f)` for `f` in `[0.1, 0.5, 0.3, 0.9, 0.7]`; `state` equals
   `.downloading(progress: 0.9)`. Then set `state = .ready` and apply
   `state = next(state, 0.95)`; `state` equals `.ready`.
6. **The engine applies it.** `let engine = HallucinationEngine()`; `engine.state` is
   `.idle`. After `engine.applyDownloadProgress(0.5)`, `engine.state` is still `.idle`.
7. **Existing behaviour intact.** `GenerationCancellationTests.swift`,
   `MetricsPanelTests.swift` and `ForcingIntensityTests.swift` are unmodified and pass,
   and the whole `ElectricSheepTests` suite is green.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    default_actor_isolation: MainActor
    protected_paths:
      - ElectricSheepTests/GenerationCancellationTests.swift
      - ElectricSheepTests/MetricsPanelTests.swift
      - ElectricSheepTests/ForcingIntensityTests.swift
      - ElectricSheep/ContentView.swift
      - ElectricSheep/HallucinationForcer.swift

## Constraints

- No new dependencies. `ModelState`, `canGenerate`, `generate`, and every other member of
  the engine are unchanged.
- The progress callback must not assign `state` directly. It calls
  `applyDownloadProgress(_:)`, and only `progressState(current:fraction:)` decides the
  new state.
- macOS is the gate, and nothing here is platform-gated. Do not add `#if os(...)`.

## Risks

- **Fixing only the race.** A guard that ignores updates outside `.downloading` but
  assigns the new fraction as-is still lets the bar move backwards. Criterion 4 pins that
  it does not.
- **Fixing only the ordering.** Taking the maximum without the `.downloading` guard turns
  `.ready` back into `.downloading`. Criterion 1 pins that it does not.
- **Capturing `Progress` in the task.** `Progress` is not Sendable. Read
  `fractionCompleted` into a local before `Task { @MainActor in ... }`, as Edit 1 does.
