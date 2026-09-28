# MetalGameOfLife: a drag that leaves the view clamps to the grid instead of crashing

Jira: DEV-312 (finding DEV-313), epic DEV-457. Repo `metal-game-of-life`, `main` = `584b38d`
(the DEV-312 SwiftPM test harness). Every fact below was read from that commit. A reference
implementation of exactly the edits below, with the tests described, ran on the Mac Studio
before this spec was written:

- `swift test` passed 7 of 7: the 1 existing test and the 6 new ones.
- `xcodebuild -project MetalGameOfLife.xcodeproj -scheme MetalGameOfLife build` succeeded.
- Against unmodified `main`, the new test file fails to compile, with
  `cannot find 'gridCell' in scope` five times.

Do not re-derive the expected values.

## Context

MetalGameOfLife is a SwiftUI and Metal app, built from `MetalGameOfLife.xcodeproj` with
`SWIFT_VERSION = 5.0`. `Package.swift` compiles the same `Sources/` directory as the SwiftPM
library `MetalGameOfLifeKit`, excluding only `MetalGameOfLifeApp.swift` (the `@main` App) and
`Shaders.metal`. Tests are Swift Testing files in `Tests/MetalGameOfLifeTests/` that use
`@testable import MetalGameOfLifeKit`. `swift test` at the repo root runs them. The renderer
needs a GPU and the app's Metal library, so tests never construct `GameOfLifeRenderer` or
`ContentView`.

Dragging on the view seeds live cells. `ContentView` maps the drag location to a grid cell:

```swift
    private func activateCells(at point: CGPoint, in viewSize: CGSize) {
        let gridX = round(point.x / viewSize.width * CGFloat(renderer.gridSize.width))
        let gridY = round(point.y / viewSize.height * CGFloat(renderer.gridSize.height))
        renderer.activateRandomCells(nearCell: CGPoint(x: gridX, y: gridY))
    }
```

and `GameOfLifeRenderer` converts that to the integer point the activation kernel reads:

```swift
    func activateRandomCells(nearCell cell: CGPoint) {
        activationPoints.append(SIMD2<UInt32>(UInt32(cell.x), UInt32(cell.y)))
    }
```

**The defect.** A `DragGesture` keeps reporting locations after the pointer leaves the view.

- Above or left of the view the location is negative, and `UInt32(cell.x)` traps. The app
  crashes (DEV-313).
- At or past the right and bottom edges, `round` yields `gridSize.width` or more: a cell one
  past the texture, which the kernel then writes.
- Before the renderer first sizes the grid, `gridSize` is 0 x 0.
- A non-finite location also traps in the conversion.

**The fix.** One pure function maps a view location to a cell. It clamps to the grid while the
value is still floating point, and returns nil when there is no cell to hit. `ContentView` calls
it and skips the activation on nil. The renderer takes the finished `SIMD2<UInt32>`, so it no
longer converts a `CGFloat` at all.

**Concurrency.** Swift language mode 5, no default actor isolation. `gridCell` is a synchronous
free function; add no actor annotations. The tests are synchronous free functions.

## Required change

### `Sources/ContentView.swift` (modified: one anchored edit and one append)

1. Replace the whole `activateCells` method, the five lines quoted in Context, with:

```swift
    private func activateCells(at point: CGPoint, in viewSize: CGSize) {
        guard let cell = gridCell(for: point, in: viewSize,
                                  gridWidth: renderer.gridSize.width,
                                  gridHeight: renderer.gridSize.height) else { return }
        renderer.activateRandomCells(at: cell)
    }
```

2. After the closing brace of `struct ContentView`, at the end of the file, add this internal
   (not `private`) free function, exactly:

```swift
/// The grid cell under `point`, a location in a view of `viewSize` that shows a
/// `gridWidth` x `gridHeight` grid. A drag gesture keeps reporting locations after
/// the pointer leaves the view, so points outside it clamp to the nearest edge cell.
/// Returns nil when there is no cell to hit: an empty grid or view (the grid is
/// 0 x 0 until the renderer first sizes it), or a non-finite coordinate.
func gridCell(for point: CGPoint, in viewSize: CGSize,
              gridWidth: Int, gridHeight: Int) -> SIMD2<UInt32>? {
    guard gridWidth > 0, gridHeight > 0,
          viewSize.width > 0, viewSize.height > 0,
          point.x.isFinite, point.y.isFinite else {
        return nil
    }
    // Clamp while still floating point: converting an out-of-range CGFloat to an
    // integer traps.
    let column = min(max((point.x / viewSize.width * CGFloat(gridWidth)).rounded(.down), 0),
                     CGFloat(gridWidth - 1))
    let row = min(max((point.y / viewSize.height * CGFloat(gridHeight)).rounded(.down), 0),
                  CGFloat(gridHeight - 1))
    return SIMD2<UInt32>(UInt32(column), UInt32(row))
}
```

`.rounded(.down)` replaces the old `round` deliberately. It picks the cell the point is inside,
and it cannot produce the one-past-the-end cell. The rest of `ContentView.swift` does not change.

### `Sources/GameOfLifeRenderer.swift` (modified: one anchored edit)

Replace the three-line `activateRandomCells(nearCell:)` method quoted in Context with:

```swift
    func activateRandomCells(at cell: SIMD2<UInt32>) {
        activationPoints.append(cell)
    }
```

Nothing else in the renderer changes.

### `Tests/MetalGameOfLifeTests/GridMappingTests.swift` (new)

A Swift Testing file with exactly these imports:

```swift
import CoreGraphics
import Testing
@testable import MetalGameOfLifeKit
```

Use a 400 x 300 view over a 200 x 150 grid, so one cell is 2 x 2 points. A private helper
`cell(_ x: CGFloat, _ y: CGFloat) -> SIMD2<UInt32>?` calls
`gridCell(for: CGPoint(x: x, y: y), in: CGSize(width: 400, height: 300), gridWidth: 200, gridHeight: 150)`.
Compare results with `==` against `SIMD2<UInt32>(x, y)` or `nil`. One `@Test` per acceptance
criterion 1 to 6.

## Change surface

The two modified files were verified to exist at `main`, and
`Tests/MetalGameOfLifeTests/GridMappingTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `Sources/ContentView.swift` | modified (one anchored edit, one append) |
| `Sources/GameOfLifeRenderer.swift` | modified (one anchored edit) |
| `Tests/MetalGameOfLifeTests/GridMappingTests.swift` | new |

Each quoted anchor occurs exactly once in its file at `main`. The protected files below are
served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does not
compile, and the report must say so.

1. **A point inside the view maps to the cell under it.** `cell(100, 75)` is `(50, 37)`;
   `cell(0, 0)` is `(0, 0)`; `cell(1.9, 1.9)` is `(0, 0)`; `cell(2, 2)` is `(1, 1)`.
2. **A drag above or left of the view clamps to the first row or column** (the DEV-313 crash).
   `cell(-40, -10)` is `(0, 0)`; `cell(-0.5, 75)` is `(0, 37)`; `cell(100, -300)` is `(50, 0)`.
3. **A drag at or past the right or bottom edge clamps to the last cell.** `cell(400, 300)` is
   `(199, 149)`; `cell(399.9, 299.9)` is `(199, 149)`; `cell(5000, 150)` is `(199, 75)`.
4. **Huge coordinates clamp without trapping.** `cell(1e12, -1e12)` is `(199, 0)`;
   `cell(-1e300, 1e300)` is `(0, 149)`.
5. **Non-finite points map to no cell.** `cell(.nan, 10)`, `cell(10, .nan)`,
   `cell(.infinity, 10)` and `cell(10, -.infinity)` are all `nil`.
6. **An empty grid or view maps to no cell.** With the point `(10, 10)`: grid 0 x 0, grid
   200 x 0, a `.zero` view, and a 400 x 0 view each give `nil`.
7. **Existing behaviour intact.** `HarnessTests.swift` is unmodified, and the whole `swift test`
   suite passes (7 tests).

## test_strategy

- framework: swift_test
- required: true
- repo: metal-game-of-life
- base_ref: main
- protected_paths:
  - Package.swift
  - Tests/MetalGameOfLifeTests/HarnessTests.swift
  - Sources/MetalView.swift
  - Sources/MetalGameOfLifeApp.swift

## Constraints

- **Create no new file under `Sources/`.** `MetalGameOfLife.xcodeproj` lists its source files
  explicitly, so a new file compiles under `swift test` but is missing from the app target, and
  the app then fails to build with `cannot find 'gridCell' in scope`. This was measured on the
  reference. `gridCell` lives in `ContentView.swift`.
- Do not edit `MetalGameOfLife.xcodeproj` or `Package.swift`.
- `gridCell` is internal, so `@testable import` can see it. It is not `private`, not
  `fileprivate`, and not a member of `ContentView`.
- No `UInt32(`, `Int(` or `Int32(` conversion of an unclamped `CGFloat` anywhere in `Sources/`.
- Tests never construct `GameOfLifeRenderer`, `ContentView` or an `MTLDevice`.

## Risks

- **Clamping after the integer conversion.** `UInt32(max(x, 0))` still traps for 1e300, and
  `Int(x)` traps for any value outside `Int`'s range. Clamp the `CGFloat`, then convert.
  Criterion 4 fails with a crash otherwise.
- **Keeping `round`.** `round(400 / 400 * 200)` is 200, one past the last column. Criterion 3
  fails.
- **Guarding only negatives.** That fixes the crash but leaves the one-past-the-end write, and
  criteria 3 and 4 fail.
- **Returning a zero cell for an empty grid.** Before the first reshape there is no cell 0, so
  the result must be `nil`. Criterion 6.
