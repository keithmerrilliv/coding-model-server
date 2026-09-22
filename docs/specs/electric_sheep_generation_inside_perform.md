# Electric Sheep: keep token generation inside `ModelContainer.perform`

Jira: DEV-605. Repo `electric-sheep`, `main` = `6070eb0` (run 54's delivery). Every fact
below was read from that commit through the runner before this spec was written; do not
re-derive them.

## Context

`ElectricSheep/HallucinationEngine.swift` (138 lines) declares
`@MainActor @Observable final class HallucinationEngine` with
`private var modelContainer: ModelContainer?`, `let generationController =
GenerationTaskController()`, `private(set) var state: ModelState`,
`private(set) var generatedText: String`, `private(set) var tokensPerSecond: Double`,
`private(set) var currentForcer: HallucinationForcer?`. `startGeneration(prompt:maxTokens:)`
hands `generate(prompt:maxTokens:)` to the controller as the single in-flight task;
`stopGeneration()` cancels it.

`generate` today (lines 88–113, verbatim):

```swift
            // Prepare input, create iterator, and start generation in one perform block
            let stream: AsyncStream<Generation> = try await container.perform { (context: ModelContext) in
                let input = try await context.processor.prepare(input: .init(prompt: prompt))
                let sampler = TopPSampler(temperature: 0.8, topP: 0.95)
                let iterator = try TokenIterator(
                    input: input,
                    model: context.model,
                    processor: forcer,
                    sampler: sampler,
                    maxTokens: maxTokens
                )

                return MLXLMCommon.generate(input: input, context: context, iterator: iterator)
            }

            for await generation in stream {
                if Task.isCancelled { break }
                switch generation {
                case .chunk(let text):
                    generatedText += text
                case .info(let info):
                    tokensPerSecond = info.tokensPerSecond
                default:
                    break
                }
            }
```

`ModelContainer` is an actor; `perform` exists to give exclusive access to the
`ModelContext`. `MLXLMCommon.generate(input:context:iterator:)` returns an
`AsyncStream<Generation>` whose internal task iterates the `TokenIterator` — which holds
`context.model` and the KV cache — and that task keeps running after `perform` has
returned and the actor's isolation is released. The model is driven from outside its
guarded region for the whole generation. Two overlapping generations, or a `loadModel`
starting while a stream is in flight, mutate the same model and cache with no
serialisation; the UI's "disable Generate unless `.ready`" is the only thing masking it.

Test-target facts: `ElectricSheepTests` links **no MLX products** (the comment on
`GenerationTaskController` says so, and that type is Foundation-only for that reason).
`ElectricSheepTests/GenerationCancellationTests.swift` is Swift Testing
(`import Testing`, `@testable import ElectricSheep`, `struct GenerationCancellationTests`,
each test `@Test("…") @MainActor func …() async`), uses `AsyncStream<Int>.makeStream()` /
`AsyncStream<Bool>.makeStream()` as started/finished signals, and constructs
`HallucinationEngine()` directly. **New tests mirror that file.**

## Goal

Consumption of the MLX generation stream moves INSIDE `perform`, so the model is only ever
driven while the actor holds it. The UI loop is unchanged in effect: it still receives
`Generation` values one at a time and updates `generatedText` / `tokensPerSecond` /
`state` exactly as today, and cancellation from `stopGeneration()` still stops the
generation. The bridge between "produced under the actor" and "consumed on the main
actor" is a small Foundation-only relay that the test target can exercise with `Int`s.

## Required change

### `ElectricSheep/StreamRelay.swift` (new, Foundation-only)

```swift
import Foundation

/// Runs `produce` in its own task and forwards what it yields to a consumer, so a
/// producer can stay inside an actor's guarded region for its whole life while the
/// consumer iterates elsewhere (DEV-605).
enum StreamRelay {
    /// Returns immediately. `produce` starts at once in a child task; every value it
    /// yields to the continuation reaches the returned stream in order; when `produce`
    /// returns the stream finishes; when it throws the stream finishes with that error.
    /// When the consumer stops iterating (returns, breaks, or is cancelled) the producer
    /// task is cancelled, so `Task.isCancelled` inside `produce` becomes true.
    static func make<Element: Sendable>(
        _ produce: @escaping @Sendable (AsyncThrowingStream<Element, Error>.Continuation) async throws -> Void
    ) -> AsyncThrowingStream<Element, Error>
}
```

Implementation shape: `AsyncThrowingStream<Element, Error>.makeStream()`, a
`Task { do { try await produce(continuation); continuation.finish() } catch {
continuation.finish(throwing: error) } }`, and `continuation.onTermination = { _ in
task.cancel() }`. No classes, no locks, no globals.

### `ElectricSheep/HallucinationEngine.swift` (modified — the `generate` body only)

Replace lines 88–113 quoted above with: build the stream through the relay, and consume
`MLXLMCommon.generate` INSIDE `perform`:

```swift
            let stream: AsyncThrowingStream<Generation, Error> = StreamRelay.make { continuation in
                try await container.perform { (context: ModelContext) in
                    let input = try await context.processor.prepare(input: .init(prompt: prompt))
                    let sampler = TopPSampler(temperature: 0.8, topP: 0.95)
                    let iterator = try TokenIterator(
                        input: input,
                        model: context.model,
                        processor: forcer,
                        sampler: sampler,
                        maxTokens: maxTokens
                    )
                    for await generation in MLXLMCommon.generate(input: input, context: context, iterator: iterator) {
                        if Task.isCancelled { break }
                        continuation.yield(generation)
                    }
                }
            }

            for try await generation in stream {
                if Task.isCancelled { break }
                switch generation {
                case .chunk(let text):
                    generatedText += text
                case .info(let info):
                    tokensPerSecond = info.tokensPerSecond
                default:
                    break
                }
            }
```

Everything else in the file — the `do`/`catch` around it, `state = .generating`, the
`if !Task.isCancelled { currentForcer = nil; state = .ready }` epilogue, the `catch`
setting `.error`, `startGeneration`, `stopGeneration`, `loadModel` — stays byte for
byte. The `perform` closure now returns `Void`; nothing built inside it escapes.

### `ElectricSheepTests/StreamRelayTests.swift` (new)

Swift Testing, the `GenerationCancellationTests.swift` shape. `Int` elements only; no
MLX type appears. Tests below.

## Change surface

Repo-relative paths. `ElectricSheep/HallucinationEngine.swift` was verified to exist at
`main`; `ElectricSheep/StreamRelay.swift` and `ElectricSheepTests/StreamRelayTests.swift`
verified NOT to exist. The plan's implement phase lists exactly these three as outputs.
Both new files go in those exact directories (synchronized root groups: a file elsewhere
is never compiled).

| Path | Change |
| --- | --- |
| `ElectricSheep/HallucinationEngine.swift` | modified (the `generate` body) |
| `ElectricSheep/StreamRelay.swift` | new |
| `ElectricSheepTests/StreamRelayTests.swift` | new |

The three protected files below are served read-only; do not edit them.

## Acceptance criteria

A green build is NOT sufficient — the tests are the gate. Criteria 1–4 do not compile on
`main` (`StreamRelay` does not exist); say so in the report.

1. **Order and completion.** `StreamRelay.make { c in c.yield(1); c.yield(2); c.yield(3) }`
   iterated with `for try await` collects exactly `[1, 2, 3]` and the loop ends.
2. **A producer error ends the stream with that error.** A producer that yields `1` and
   then throws a test-local `enum RelayTestError: Error { case boom }` value: the consumer
   collects `[1]`, and the error caught from the `for try await` is `RelayTestError.boom`
   (compare with `if case RelayTestError.boom = error`).
3. **`make` returns before the producer finishes.** A producer that first awaits a
   `gate` (`AsyncStream<Int>.makeStream()`; `for await _ in gate.stream { break }`) and only
   then yields `7`: `let stream = StreamRelay.make { … }` returns, the test then calls
   `gate.continuation.yield(0)`, and iterating `stream` collects `[7]`.
4. **Stopping the consumer cancels the producer.** A producer that yields `1`, then loops
   `while !Task.isCancelled { try? await Task.sleep(for: .milliseconds(5)) }`, then yields
   `true` into a `finished` `AsyncStream<Bool>.makeStream()` continuation and returns.
   The consumer, in a child `Task`, breaks out of `for try await` after the first element;
   the test then cancels that child task and awaits it, and `for await done in
   finished.stream { #expect(done); break }` completes — the producer observed
   `Task.isCancelled` (the same started/finished pattern as
   `GenerationCancellationTests.cancelStopsInFlightBody`).
5. **The engine's stream never leaves the actor.** Inspection of
   `ElectricSheep/HallucinationEngine.swift`: `container.perform` appears exactly once in
   `generate`, its closure contains `for await generation in MLXLMCommon.generate(` and
   `continuation.yield(generation)`, and the text `return MLXLMCommon.generate` is gone;
   `for try await generation in stream` drives the same `switch` as before.
6. **Existing behaviour intact.** The three `GenerationCancellationTests` pass unchanged;
   the whole `ElectricSheepTests` suite is green under the skip filter below.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    skip_filter: ElectricSheepTests/DtypeContainmentTests,ElectricSheepTests/ProductionDtypeConversionTests
    default_actor_isolation: MainActor
    protected_paths:
      - ElectricSheep/GenerationTaskController.swift
      - ElectricSheepTests/GenerationCancellationTests.swift
      - ElectricSheep/HallucinationForcer.swift

## Constraints

- No new dependencies. `StreamRelay.swift` imports Foundation and nothing else; it names
  no MLX type, so the test target can use it.
- `HallucinationEngine.swift` changes only inside `generate`'s `do` block as quoted; the
  file's other declarations are untouched.
- No `.metal`, no `#if os(...)`, nothing platform-gated: macOS is the gate.

## Risks

- **Sendability.** `produce` is `@Sendable`; it captures `container` (an actor,
  Sendable), `forcer` (`HallucinationForcer: @unchecked Sendable`), `prompt` and
  `maxTokens` (values). Capture nothing else.
- **Yielding after termination.** `continuation.yield` after the consumer stopped is a
  no-op, not an error; the `Task.isCancelled` check inside the `perform` loop is what
  stops the model.
- **A blocking relay.** `make` must not `await`; a relay that runs the producer inline
  hangs criterion 3.
