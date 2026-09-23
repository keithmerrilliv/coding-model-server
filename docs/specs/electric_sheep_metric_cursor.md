# Electric Sheep: the metric cursor keeps working past 200 tokens

Jira: DEV-813, epic DEV-443. Repo `electric-sheep`, `main` = `a0f8811` (run 58's
delivery). Every fact below was read from that commit, and every expected value in the
acceptance criteria was computed by simulating both the current and the fixed algorithm
before this spec was written; do not re-derive them.

## Context

`ElectricSheep/HallucinationForcer.swift` records one `TokenMetrics` per generated token
into `_allMetrics`, capped at `static let maxMetrics = 200` by trimming the front on every
append. `MetricsParticleBridge.update(...)` is the only production consumer: at 90 Hz it
calls `forcer.consumeNewMetrics()` and spawns one particle and one audio strike per metric
returned.

The cursor is an index into the trimmed array. Current code, verbatim:

```swift
    /// Monotonic cursor into `_allMetrics`. Protected by `lock`.
    private var consumedCount: Int = 0
```

```swift
    /// Returns unconsumed metrics capped at 5, advances cursor to current count.
    func consumeNewMetrics() -> [TokenMetrics] {
        return lock.withLock {
            if consumedCount >= _allMetrics.count || _allMetrics.isEmpty {
                return []
            }
            // Clamp start index to valid range (handles truncation edge case)
            let startIndex = Swift.min(consumedCount, _allMetrics.count - 1)
            let available = Array(_allMetrics[startIndex...])
            let taken = available.prefix(5)
            // Advance cursor past all currently known metrics
            consumedCount = _allMetrics.count
            return Array(taken)
        }
    }
```

**The defect.** After 200 metrics the consumer sets `consumedCount = 200`. The 201st
append trims the array back to 200, so `consumedCount >= _allMetrics.count` is `200 >= 200`
and the function returns `[]` — and does so for every metric after it. From token 201 on,
no particle spawns and no strike sounds, for the rest of the generation. It is hidden today
only because the default `maxTokens` is 200.

**A second mismatch in the same function.** `prefix(5)` returns the OLDEST five
unconsumed metrics. `BridgeLifecycleTests.test_window_cap_and_cursor_advance` documents
"only the newest 5 are reachable" but asserts only the count. For a live visualisation the
newest five are right; this spec makes that the behaviour and pins it.

There are **two** append sites, each followed by the same trim, and they differ only in
the variable appended and their indentation. In `didSample(token:)` (16-space indent):

```swift
                _allMetrics.append(pending)
                if _allMetrics.count > Self.maxMetrics {
                    _allMetrics.removeFirst(_allMetrics.count - Self.maxMetrics)
                }
```

and in the test helper `record(metric:)` at the bottom of the file (12-space indent):

```swift
            _allMetrics.append(metric)
            if _allMetrics.count > Self.maxMetrics {
                _allMetrics.removeFirst(_allMetrics.count - Self.maxMetrics)
            }
```

**Anchor each edit on its `append(...)` line** — `append(pending)` and `append(metric)` —
never on the `if` block alone, which occurs twice and would make a SEARCH ambiguous.

**Isolation.** The app target sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor` and no
declaration in `HallucinationForcer.swift` carries an isolation annotation, so every member
shares the class's default. The new private helper below must carry **no** annotation
either — not `nonisolated`, not `@MainActor` — so it shares the same isolation as the
stored properties it mutates and the methods that call it.

## Required change

### `ElectricSheep/HallucinationForcer.swift` (modified — six anchored edits)

**Edit 1.** Replace the two lines

```swift
    /// Monotonic cursor into `_allMetrics`. Protected by `lock`.
    private var consumedCount: Int = 0
```

with

```swift
    /// Metrics appended since the last `prompt(_:)`, INCLUDING ones since trimmed from
    /// `_allMetrics`. Monotonic, so trimming cannot move it (DEV-813). Protected by `lock`.
    private var totalAppended: Int = 0

    /// How many of `totalAppended` the consumer has taken. Same monotonic space.
    /// Protected by `lock`.
    private var consumedCount: Int = 0
```

**Edit 2.** Replace the whole of `consumeNewMetrics()`, shown verbatim above, with

```swift
    /// Returns the newest unconsumed metrics, at most 5, and marks everything appended so
    /// far as consumed. Only metrics still retained in `_allMetrics` can be returned; any
    /// unconsumed ones already trimmed are skipped, never re-delivered (DEV-813).
    func consumeNewMetrics() -> [TokenMetrics] {
        return lock.withLock {
            let unconsumed = totalAppended - consumedCount
            consumedCount = totalAppended
            guard unconsumed > 0 else { return [] }
            let reachable = Swift.min(unconsumed, _allMetrics.count)
            return Array(_allMetrics.suffix(reachable).suffix(5))
        }
    }
```

**Edit 3.** In `prompt(_:)`, immediately after the line `            consumedCount = 0`, add

```swift
            totalAppended = 0
```

**Edit 4.** In `didSample(token:)`, replace the four lines beginning
`                _allMetrics.append(pending)` (shown above) with

```swift
                appendLocked(pending)
```

**Edit 5.** In `record(metric:)`, replace the four lines beginning
`            _allMetrics.append(metric)` (shown above) with

```swift
            appendLocked(metric)
```

**Edit 6.** Immediately after the closing brace of `consumedMetricCount` (the
`var consumedMetricCount: Int { lock.withLock { consumedCount } }` property), add

```swift

    /// Appends one metric and trims to `maxMetrics`. The caller MUST already hold `lock`.
    /// The single append path, so the two call sites cannot drift apart again (DEV-813).
    private func appendLocked(_ metric: TokenMetrics) {
        _allMetrics.append(metric)
        totalAppended += 1
        if _allMetrics.count > Self.maxMetrics {
            _allMetrics.removeFirst(_allMetrics.count - Self.maxMetrics)
        }
    }
```

Nothing else in the file changes. `consumedMetricCount` keeps its body; it now reports the
monotonic count, which the existing tests already expect (12 after 12, 7 after 7).

### `ElectricSheepTests/MetricCursorTests.swift` (new)

XCTest, mirroring `BridgeLifecycleTests.swift`: `import Foundation`, `import XCTest`,
`@testable import ElectricSheep`, `import MLX` (for `prompt(_:)`'s `MLXArray`), and
`final class MetricCursorTests: XCTestCase` whose test methods are each
`@MainActor func test_...() async throws`. `HallucinationForcer` is main-actor by the
target's default isolation.

`BridgeLifecycleTests`' helpers are nested in that class; do not reuse them. Declare these
file-private ones, verbatim:

```swift
private struct CursorStubStrategy: ForcingStrategy, Sendable {
    let hallucinationType: HallucinationType = .factualErrors
    func corrupt(logits: MLXArray, context: ForcingContext) -> MLXArray { logits }
}

private func metric(_ id: Int) -> TokenMetrics {
    TokenMetrics(
        tokenID: id,
        tokenString: "t\(id)",
        originalTop1Probability: 0.9,
        modifiedTop1Probability: 0.85,
        klDivergence: 0.2,
        entropy: 1.0,
        activeStrategy: .factualErrors,
        confidenceGap: 0.05,
        timestamp: 0
    )
}
```

Each test makes `HallucinationForcer(strategy: CursorStubStrategy())` and uses
`record(metric:)` and `consumeNewMetrics()`. Compare token IDs with
`.map(\.tokenID)`.

## Change surface

`ElectricSheep/HallucinationForcer.swift` was verified to exist at `main`;
`ElectricSheepTests/MetricCursorTests.swift` verified NOT to exist. The plan's implement
phase lists exactly these two as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/HallucinationForcer.swift` | modified (six anchored edits) |
| `ElectricSheepTests/MetricCursorTests.swift` | new |

The three protected files below are served read-only; do not edit them.

## Acceptance criteria

A green build is NOT sufficient — the tests are the gate. Criteria 1, 2, 3, 4 and 6 FAIL on
`main`; criterion 5 passes on `main` too and is a regression guard. Say which in the report.

1. **The cursor survives the cap.** Record ids 0...199; the first consume returns ids
   `[195, 196, 197, 198, 199]`. Record id 200; the next consume returns exactly `[200]`.
   On `main` the first returns `[0, 1, 2, 3, 4]` and the second returns `[]`.
2. **It keeps working.** Record ids 0...199 and consume once. Then 100 times: record id
   `200 + i`, consume, and assert the result is exactly `[200 + i]`. On `main` all 100 are
   empty.
3. **A backlog returns the newest, then resumes.** Record ids 0...249 without consuming;
   consume returns `[245, 246, 247, 248, 249]` and `consumedMetricCount == 250`. Record id
   250; consume returns `[250]` and `consumedMetricCount == 251`. On `main` the first is
   `[50, 51, 52, 53, 54]` and the second is `[]`.
4. **The window cap takes the newest five.** Record ids 0...11; consume returns
   `[7, 8, 9, 10, 11]`, `consumedMetricCount == 12`, and a second consume returns `[]`.
   On `main` the first is `[0, 1, 2, 3, 4]`.
5. **`prompt(_:)` still resets.** Record ids 0...249, consume, call
   `prompt(MLXArray([Float](repeating: 0, count: 32)))`, record ids 1000 and 1001; consume
   returns `[1000, 1001]` and `consumedMetricCount == 2`. Passes on `main` too.
6. **Exactly once, with no loss, far past the cap.** In 167 rounds, record three new ids
   (0...500 in order) then consume. The concatenation of every result equals exactly
   `Array(0...500)` — 501 ids, each once, in order. On `main` only 200 come back.
7. **Existing behaviour intact.** `BridgeLifecycleTests.swift` is unmodified and its four
   tests pass; the whole `ElectricSheepTests` suite is green.

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
      - ElectricSheepTests/BridgeLifecycleTests.swift
      - ElectricSheep/MetricsParticleBridge.swift
      - ElectricSheep/TokenMetrics.swift

## Constraints

- No new dependencies. No change to `TokenMetrics`, `MetricsParticleBridge` or the
  public signature of any `HallucinationForcer` member.
- `maxMetrics` stays 200. The fix is the cursor, not a bigger buffer.
- `appendLocked` carries no isolation annotation and is only ever called while `lock` is
  held.
- macOS is the gate; nothing here is platform-gated. Do not add `#if os(...)`.

## Risks

- **An ambiguous SEARCH.** The trim `if` block appears twice. Anchor on
  `_allMetrics.append(pending)` and `_allMetrics.append(metric)`.
- **Re-delivery after a trim.** If more than 200 metrics arrive between consumes, the ones
  trimmed before being read are skipped, never re-delivered. `reachable` enforces that;
  criterion 3 pins it.
- **Forgetting `totalAppended = 0` in `prompt(_:)`.** The cursor would then carry across
  runs; criterion 5 catches it.
- **`prefix` for `suffix`.** Criteria 1, 3 and 4 fail if the oldest five are returned.
