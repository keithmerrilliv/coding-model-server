# Electric Sheep: scope the MLX failure handler so concurrent tests stop clobbering each other

Jira: DEV-603. Repo `electric-sheep`, `main` = `80bcfce` (run 55's delivery). Every fact
below was read from that commit through the runner, or measured against it on the Mac
runner on 2026-09-22, before this spec was written; do not re-derive them.

## Context

`ElectricSheep/DtypeValidation.swift` is 1,113 characters and holds the whole containment
path, verbatim:

```swift
import MLX
import Foundation

struct MLXDtypeError: Error, CustomStringConvertible, Sendable {
    let actualDtype: String

    var description: String {
        "Forcing strategy requires .float32 logits, but received \(actualDtype)."
    }
}

final class MLXFailureRouter: @unchecked Sendable {
    static let shared = MLXFailureRouter()

    private let lock = NSLock()
    private var handler: ((MLXDtypeError) -> Void)?

    func install(_ newHandler: ((MLXDtypeError) -> Void)?) {
        lock.lock()
        defer { lock.unlock() }
        self.handler = newHandler
    }

    func report(_ error: MLXDtypeError) {
        lock.lock()
        let currentHandler = handler
        lock.unlock()

        if let h = currentHandler {
            h(error)
        } else {
            print("MLX Failure: \(error.description)")
        }
    }
}

func logitsAreFloat32(_ logits: MLXArray) -> Bool {
    guard logits.dtype == .float32 else {
        MLXFailureRouter.shared.report(MLXDtypeError(actualDtype: String(describing: logits.dtype)))
        return false
    }
    return true
}
```

`MLXFailureRouter.shared` is a process-global singleton with exactly **one handler slot**.
`lock` keeps the slot from tearing; it does nothing about last-writer-wins.

All four tests in `ElectricSheepTests/DtypeContainmentTests.swift` share that one slot the
same way — `MLXFailureRouter.shared.install { … }` followed by
`defer { MLXFailureRouter.shared.install(nil) }`. Swift Testing runs `@Test` functions
concurrently, so two overlapping tests clobber each other: one test's `install` replaces
the other's handler, one test's `defer` blanks it mid-flight, and a report raised by one
test is counted by another.

### This was measured, not inferred

Six dispatches of the unmodified `main` worktree (`patch_files: []`, no change under test)
filtered to `ElectricSheepTests/DtypeContainmentTests`, and three filtered to `a1` alone:

| Filter | Runs | Red |
| --- | --- | --- |
| `DtypeContainmentTests` (a1 and a2 together) | 6 | 3 |
| `a1_float64_input_rejected_with_readable_error()` alone | 3 | 0 |

a1 in isolation is green every time. a1 beside a2 is red half the time. a1 and a2 always
fail as a pair, because a2 steals a1's captured error and a1's report inflates a2's
counter.

Exactly two tests ever reach `report`: a1 and `unsupportedDtypeStillContained`, both by
driving Float64. Exactly four ever call `install`, all four in that one file.
`ProductionLogitShapeTests` in `ElectricSheepTests/ForcingStrategyTests.swift` drives
float16 and bfloat16, which DEV-585 made convert rather than report, so it is not a third
racer. The hazard is entirely inside `DtypeContainmentTests.swift`.

## Goal

A scoped, mutually exclusive way to observe failures: install a handler for the duration of
a block, restore whatever was installed before rather than clearing it, and hold a gate so
two callers can never observe each other's reports. The four existing tests move onto it
and keep asserting exactly what they assert today. The invariant becomes testable, which
today it is not.

## Required change

### `ElectricSheep/DtypeValidation.swift` (modified — two insertions, nothing else)

1. Immediately after the line `    private let lock = NSLock()` add:

   ```swift
       /// Serialises whole `withHandler` bodies. Recursive so a block may nest
       /// inside another on the same thread without deadlocking (DEV-603).
       private let gate = NSRecursiveLock()
   ```

2. Immediately after the closing brace of `install(_:)` — that is, between `install` and
   `func report(_ error: MLXDtypeError) {` — add:

   ```swift
       /// Runs `body` with `newHandler` installed and restores the handler that was
       /// installed before, holding `gate` for the whole call so two concurrent callers
       /// can never observe each other's reports (DEV-603).
       ///
       /// `body` must not suspend: the gate is a thread lock, so an `await` inside it
       /// would release the thread, not the lock.
       func withHandler<T>(_ newHandler: @escaping (MLXDtypeError) -> Void,
                           _ body: () throws -> T) rethrows -> T {
           gate.lock()
           defer { gate.unlock() }
           let previous = currentHandler()
           install(newHandler)
           defer { install(previous) }
           return try body()
       }

       /// The handler installed right now, read under `lock`.
       private func currentHandler() -> ((MLXDtypeError) -> Void)? {
           lock.lock()
           defer { lock.unlock() }
           return handler
       }
   ```

`install(_:)`, `report(_:)`, `MLXDtypeError` and `logitsAreFloat32` stay byte for byte.
Anchor both SEARCH blocks on the lines quoted above; both are unique in the file.

Deferred blocks run in reverse order of declaration, so `install(previous)` runs before
`gate.unlock()`. That ordering is required: releasing the gate first would let another
thread in while the handler is still the block's own.

### `ElectricSheepTests/DtypeContainmentTests.swift` (modified — re-emit in full)

Replace the file with exactly this. It is the current file with each of the four tests
moved onto `withHandler` and nothing else changed; every `#expect` is the one that is
there today.

```swift
import Testing
import MLX
@testable import ElectricSheep

@Suite
struct DtypeContainmentTests {

    @Test func a1_float64_input_rejected_with_readable_error() {
        var capturedError: String?
        MLXFailureRouter.shared.withHandler({ capturedError = $0.description }) {
            let input = MLXArray([3.0, 5.0, 1.0, 4.0]) // Float64
            let output = RestrictedSamplingStrategy().corrupt(logits: input, context: ForcingContext(tokenIndex: 0, recentTokens: []))

            #expect(capturedError?.contains("float64") == true)
            #expect(output.dtype == input.dtype)
            #expect(capturedError != nil)
        }
    }

    @Test func a2_all_strategies_process_valid_float32_correctly() {
        let strategies = HallucinationType.allCases.map { $0.makeStrategy() }
        #expect(strategies.count == 8)
        var errorCount = 0
        MLXFailureRouter.shared.withHandler({ _ in errorCount += 1 }) {
            for strategy in strategies {
                let logits = MLXArray(Array(repeating: Float(1.0), count: 10))
                let result = strategy.corrupt(logits: logits, context: ForcingContext(tokenIndex: 0, recentTokens: [1]))
                let values: [Float] = result.asArray(Float.self)
                #expect(values.allSatisfy { $0.isFinite })
            }
        }
        #expect(errorCount == 0)
    }
}
// MARK: - DEV-585: f16/bf16 are converted, not rejected

@Suite("Production dtype conversion (DEV-585)")
struct ProductionDtypeConversionTests {

    private static func logits(_ dtype: DType) -> MLXArray {
        MLXArray(Array(repeating: Float(1.0), count: 64))
            .reshaped([1, 64])
            .asType(dtype)
    }

    @Test("float16 and bfloat16 no longer report to the failure router")
    func productionDtypesDoNotReport() {
        for dtype in [DType.float16, DType.bfloat16] {
            var captured: String?
            MLXFailureRouter.shared.withHandler({ captured = $0.description }) {
                let forcer = HallucinationForcer(strategy: GaussianNoiseStrategy())
                _ = forcer.process(logits: Self.logits(dtype))
            }
            #expect(captured == nil,
                    "\(dtype) is a production dtype and must be converted, not reported")
        }
    }

    // NOTE: this exercises the strategy directly, NOT HallucinationForcer.process.
    // process() computes softmax(logits) and .item(Float.self) before any dtype
    // handling, so float64 traps there and never reaches the containment guard —
    // the guard only protects a strategy called directly. Driving float64 through
    // process() crashes the test host and takes the rest of the suite with it.
    @Test("float64 still reports and still passes through unchanged")
    func unsupportedDtypeStillContained() {
        var captured: String?
        let input = MLXArray([3.0, 5.0, 1.0, 4.0])   // Float64
        MLXFailureRouter.shared.withHandler({ captured = $0.description }) {
            let out = GaussianNoiseStrategy().corrupt(
                logits: input, context: ForcingContext(tokenIndex: 0, recentTokens: []))

            #expect(captured != nil, "float64 must still be reported")
            #expect(out.dtype == input.dtype, "float64 must pass through unchanged")
        }
    }
}
```

### `ElectricSheepTests/FailureRouterScopeTests.swift` (new)

Swift Testing, the shape of the file above: `import Testing`, `@testable import
ElectricSheep`, a `@Suite` struct, plain synchronous `@Test func` methods, **no
`@MainActor`, no `async`**. It also needs `import Foundation` for
`DispatchQueue.concurrentPerform`. It must name no MLX type and must not `import MLX`:
raise failures with `MLXFailureRouter.shared.report(MLXDtypeError(actualDtype: "float64"))`
directly, which is the same entry point `logitsAreFloat32` uses.

Given code — a thread-safe collector the concurrency test needs, file-private:

```swift
private final class Counts: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Int] = []

    func record(_ n: Int) {
        lock.lock()
        defer { lock.unlock() }
        values.append(n)
    }

    var recorded: [Int] {
        lock.lock()
        defer { lock.unlock() }
        return values
    }
}
```

## Change surface

Repo-relative paths. `ElectricSheep/DtypeValidation.swift` and
`ElectricSheepTests/DtypeContainmentTests.swift` were verified to exist at `main`;
`ElectricSheepTests/FailureRouterScopeTests.swift` verified NOT to exist. The plan's
implement phase lists exactly these three as outputs. The new test file goes in
`ElectricSheepTests/` (the project uses synchronized root groups; a file elsewhere is never
compiled).

| Path | Change |
| --- | --- |
| `ElectricSheep/DtypeValidation.swift` | modified (two insertions) |
| `ElectricSheepTests/DtypeContainmentTests.swift` | modified (re-emitted in full, as given) |
| `ElectricSheepTests/FailureRouterScopeTests.swift` | new |

The three protected files below are served read-only; do not edit them.

## Acceptance criteria

A green build is NOT sufficient — the tests are the gate. Criteria 1 to 4 do not compile on
`main` (`withHandler` does not exist); say so in the report.

1. **The previous handler is restored, not cleared.** Two nested `withHandler` blocks, an
   outer collecting into `outer: [String]` and an inner into `inner: [String]`. Raise one
   failure inside the inner block and one in the outer block after the inner has returned.
   `inner.count == 1` and `outer.count == 1`. The second half is the point: the
   `install(nil)` idiom this replaces would have cleared the outer handler when the
   inner scope ended, leaving `outer` empty.
2. **A block's reports reach only that block's handler.** In the same test, the inner
   block's failure must not appear in `outer`, so `outer.first` is the failure raised
   after the inner block returned, not the one raised inside it. Raise them with distinct
   `actualDtype` values (`"inner"` and `"outer"`) and compare on
   `description.contains(…)`.
3. **Nesting on one thread does not deadlock.** Criterion 1 completing at all is the
   assertion; the gate must be an `NSRecursiveLock`.
4. **Concurrent blocks never interleave.** `DispatchQueue.concurrentPerform(iterations: 8)`,
   each iteration entering its own `withHandler` whose handler increments a local `seen`,
   raising exactly 25 failures inside the block, then recording `seen` into the `Counts`
   collector above. `counts.recorded.count == 8` and
   `counts.recorded.allSatisfy { $0 == 25 }`. Without the gate the counts scatter.
5. **Inspection.** `ElectricSheepTests/DtypeContainmentTests.swift` contains no
   `.install(` call at all and contains `withHandler` exactly four times;
   `ElectricSheep/DtypeValidation.swift` still contains `func install(` and
   `func report(` unchanged.
6. **Existing behaviour intact.** The four tests in `DtypeContainmentTests.swift` pass,
   asserting exactly what they assert on `main`, and the whole `ElectricSheepTests` suite
   is green — including `DtypeContainmentTests` and `ProductionDtypeConversionTests`,
   which are deliberately NOT skipped by this spec.

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
      - ElectricSheepTests/ForcingStrategyTests.swift
      - ElectricSheep/HallucinationForcer.swift
      - ElectricSheep/Protocols.swift

## Constraints

- No new dependencies, no `.metal`, no changes outside the three paths. There is no
  `skip_filter` here on purpose: the two dtype suites must run, or the fix proves nothing.
- `FailureRouterScopeTests.swift` must not `import MLX` and must name no MLX type. It
  raises failures through `MLXFailureRouter.shared.report(_:)`.
- Nothing inside a `withHandler` body may `await` or suspend. All four converted tests and
  all new tests are synchronous.
- macOS is the gate: nothing here is platform-gated; do not add `#if os(...)`.
- `Self.` on every static reference (the diagnostic `static member '…' cannot be used on
  instance of type` means ADD the qualifier; DEV-764).

## Risks

- **Reaching for `.serialized` instead.** A Swift Testing trait would serialise the two
  suites but leaves the one-slot global in place and pins nothing, so criteria 1, 2 and 4
  would have nothing to test. The fix belongs in `MLXFailureRouter`.
- **A non-recursive gate.** `NSLock` deadlocks the nested blocks in criterion 1. It must be
  `NSRecursiveLock`.
- **Defer order.** Declaring `defer { install(previous) }` before `defer { gate.unlock() }`
  reverses the unwind and releases the gate while the block's handler is still installed.
- **Rewriting `DtypeValidation.swift` instead of inserting.** The change is two insertions;
  a whole-file re-emission that drifts anywhere else fails criterion 5.
- **`@MainActor` on the new tests.** `MLXFailureRouter` is `@unchecked Sendable` and is
  used from background threads; adding `@MainActor` or `async` breaks
  `DispatchQueue.concurrentPerform` in criterion 4.
