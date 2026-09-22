# Centipede renderer — slice 3: the shot and the poisoned mushroom reach the screen

Jira: DEV-808, under epic DEV-628. Predecessors: DEV-765 (slice 1, run 49) and DEV-788
(slice 2, run 53), which named this as its deferred work.
Repo `centipede`, `main` = `5c9fa53` (run 53's delivery, slice 2).
Every fact below was read from that commit before this spec was written; do not re-derive them.

## Context

Slice 1 built `OffscreenRenderer`, slice 2 built the `BoardSnapshot` → `FrameSnapshot`
adapter. Two drawable things still never reach the screen.

`Sources/CentipedeCore/BoardSnapshot.swift` publishes seven entity kinds:

```swift
public enum BoardEntity: Equatable, Sendable {
    case mushroom(damage: Int, poisoned: Bool)
    case segment(isHead: Bool)
    case player
    case shot
    case spider
    case flea
    case scorpion
}
```

`Sources/CentipedeRender/FrameSnapshot.swift` publishes six, with no shot and no
poison:

```swift
public enum CellKind: Equatable, Sendable {
    case mushroom(damage: Int)
    case segment(isHead: Bool)
    case player
    case spider
    case flea
    case scorpion
}
```

So `Sources/CentipedeRender/BoardAdapter.swift` **throws both away**, verbatim:

```swift
            case .mushroom(let damage, _):
                cells[gridPos] = .mushroom(damage: damage)
            ...
            case .shot:
                continue
```

The `_` is the `poisoned` flag and `continue` is the shot. `Game.boardSnapshot()` does
populate both — it writes `.shot` for `activeShotValue` and passes `mush.poisoned`
through — so the loss is entirely in the render layer.

**Damage is 0...3 and the four-entry ramp is exactly right.** `World.swift:109` removes
a mushroom once `damageLevel` reaches 4 (`if mush.damageLevel < 4`), so a mushroom that
appears on a board always indexes `Palette.mushroom` in range. The same holds for the
poisoned ramp this spec adds.

## Goal

A fired shot and a poisoned mushroom are visible, each in its own colour. Nothing that
renders today changes colour or position.

## Required change

### `Sources/CentipedeRender/FrameSnapshot.swift` (modified — two inserted cases)

`CellKind` becomes exactly:

```swift
public enum CellKind: Equatable, Sendable {
    case mushroom(damage: Int)
    case poisonedMushroom(damage: Int)
    case segment(isHead: Bool)
    case player
    case shot
    case spider
    case flea
    case scorpion
}
```

**`case mushroom(damage: Int)` keeps its exact shape.** A poisoned mushroom is a separate
case, not a second associated value, precisely so every existing `.mushroom(damage: 0)`
call site still compiles. `GridPosition` and `FrameSnapshot` are untouched.

### `Sources/CentipedeRender/BoardAdapter.swift` (modified — two branches)

Replace the mushroom branch:

```swift
            case .mushroom(let damage, let poisoned):
                cells[gridPos] = poisoned ? .poisonedMushroom(damage: damage)
                                          : .mushroom(damage: damage)
```

and replace the shot branch (`case .shot:` followed by `continue`) with:

```swift
            case .shot:
                cells[gridPos] = .shot
```

Every other branch, and the `init` signature, stays byte for byte.

### `Sources/CentipedeRender/OffscreenRenderer.swift` (modified — two insertions)

1. Immediately after the closing `]` of the existing `public static let mushroom: [RGBA]`
   array, add:

   ```swift
       public static let poisonedMushroom: [RGBA] = [
           RGBA(r: 90,  g: 90,  b: 255, a: 255),
           RGBA(r: 70,  g: 70,  b: 200, a: 255),
           RGBA(r: 50,  g: 50,  b: 150, a: 255),
           RGBA(r: 30,  g: 30,  b: 100, a: 255),
       ]

       public static let shot = RGBA(r: 255, g: 255, b: 180, a: 255)
   ```

2. In `color(for:)`, immediately after the two lines

   ```swift
           case .mushroom(let damage):
               return mushroom[damage]
   ```

   add:

   ```swift
           case .poisonedMushroom(let damage):
               return poisonedMushroom[damage]
           case .shot:
               return shot
   ```

Nothing else in the file changes — not the vertex builder, not the shader source, not the
render pass. The switch becomes exhaustive again by these two branches alone.

### `Tests/CentipedeRenderTests/BoardAdapterTests.swift` (modified — re-emit in full)

**This file currently pins the OLD contract**: its first test is named
`adapterMapsEveryDrawableKindAndDropsShot`, asserts `frame.cells.count == 7`, and asserts
the shot cell is `nil`. All three become wrong. Replace the file with exactly this — it is
the current file with that one test corrected and nothing else touched:

```swift
import Testing
import CentipedeCore
import CentipedeRender

struct BoardAdapterTests {

    @Test func adapterMapsEveryDrawableKindIncludingShotAndPoison() {
        let mushPos = BoardCell(column: 1, row: 1)
        let headPos = BoardCell(column: 2, row: 2)
        let bodyPos = BoardCell(column: 3, row: 3)
        let playerPos = BoardCell(column: 4, row: 4)
        let shotPos = BoardCell(column: 5, row: 5)
        let spiderPos = BoardCell(column: 6, row: 6)
        let fleaPos = BoardCell(column: 7, row: 7)
        let scorpionPos = BoardCell(column: 8, row: 8)
        let plainMushPos = BoardCell(column: 9, row: 9)

        let board = BoardSnapshot(entities: [
            mushPos: .mushroom(damage: 2, poisoned: true),
            headPos: .segment(isHead: true),
            bodyPos: .segment(isHead: false),
            playerPos: .player,
            shotPos: .shot,
            spiderPos: .spider,
            fleaPos: .flea,
            scorpionPos: .scorpion,
            plainMushPos: .mushroom(damage: 1, poisoned: false)
        ])

        let frame = FrameSnapshot(board: board)
        #expect(frame.columns == 30)
        #expect(frame.rows == 30)
        #expect(frame.cells.count == 9)

        #expect(frame.cells[GridPosition(column: 1, row: 1)] == .poisonedMushroom(damage: 2))
        #expect(frame.cells[GridPosition(column: 2, row: 2)] == .segment(isHead: true))
        #expect(frame.cells[GridPosition(column: 3, row: 3)] == .segment(isHead: false))
        #expect(frame.cells[GridPosition(column: 4, row: 4)] == .player)
        #expect(frame.cells[GridPosition(column: 5, row: 5)] == .shot)
        #expect(frame.cells[GridPosition(column: 6, row: 6)] == .spider)
        #expect(frame.cells[GridPosition(column: 7, row: 7)] == .flea)
        #expect(frame.cells[GridPosition(column: 8, row: 8)] == .scorpion)
        #expect(frame.cells[GridPosition(column: 9, row: 9)] == .mushroom(damage: 1))
    }

    @Test func adaptedBoardRendersWhereBoardSaid() throws {
        let board = BoardSnapshot(entities: [
            BoardCell(column: 0, row: 0): .segment(isHead: true),
            BoardCell(column: 0, row: 29): .player
        ])
        let frame = FrameSnapshot(board: board)
        let renderer = try #require(OffscreenRenderer())
        let result = try #require(renderer.render(frame, width: 300, height: 300))
        #expect(result.pixel(x: 5, y: 5) == Palette.head)
        #expect(result.pixel(x: 5, y: 295) == Palette.player)
        #expect(result.pixel(x: 150, y: 150) == Palette.background)
    }
}
```

Nine entities in, nine cells out. The second test is unchanged.

### `Tests/CentipedeRenderTests/ShotAndPoisonTests.swift` (new)

Swift Testing, the shape of the file above: `import Testing`, `import CentipedeCore`,
`import CentipedeRender`, a plain `struct ShotAndPoisonTests`, `@Test func` methods. A
test that calls `try #require` must be declared `throws` (DEV-791). Tests below.

## Change surface

Repo-relative paths. The four modified files were verified to exist at `main`;
`Tests/CentipedeRenderTests/ShotAndPoisonTests.swift` verified NOT to exist. The plan's
implement phase lists exactly these five as outputs.

| Path | Change |
| --- | --- |
| `Sources/CentipedeRender/FrameSnapshot.swift` | modified (two inserted cases) |
| `Sources/CentipedeRender/BoardAdapter.swift` | modified (two branches) |
| `Sources/CentipedeRender/OffscreenRenderer.swift` | modified (two insertions) |
| `Tests/CentipedeRenderTests/BoardAdapterTests.swift` | modified (re-emitted as given) |
| `Tests/CentipedeRenderTests/ShotAndPoisonTests.swift` | new |

The three protected files below are served read-only; do not edit them.

## Acceptance criteria

A green build is NOT sufficient — the tests are the gate. Criteria 1 to 5 do not compile
on `main` (`.shot` and `.poisonedMushroom` do not exist as `CellKind` cases); say so in
the report.

1. **The shot reaches the frame.** A `BoardSnapshot` whose only entity is `.shot` at
   `BoardCell(column: 5, row: 5)` adapts to a `FrameSnapshot` where
   `cells[GridPosition(column: 5, row: 5)] == .shot` and `cells.count == 1`. On `main` the
   adapter drops it and the count is 0.
2. **Poison is carried, and only when set.** `.mushroom(damage: 3, poisoned: true)` adapts
   to `.poisonedMushroom(damage: 3)`; `.mushroom(damage: 3, poisoned: false)` adapts to
   `.mushroom(damage: 3)`. Both in one test, both damage values preserved.
3. **The palette answers for both.** `Palette.color(for: .shot) == Palette.shot`, and for
   every `d` in `0..<4`, `Palette.color(for: .poisonedMushroom(damage: d)) ==
   Palette.poisonedMushroom[d]`.
4. **Every palette colour is distinct.** Collect all sixteen — `background`, the four
   `mushroom`, the four `poisonedMushroom`, `shot`, `head`, `body`, `player`, `spider`,
   `flea`, `scorpion` — into one array and assert the array has 16 elements and
   `Set(...).count == 16`. (1 + 4 + 4 + 1 + 6 = 16; count the array as well as the set, or
   a missing entry passes silently.) A renderer whose
   colours collide cannot be tested by pixel, so this guards every other pixel assertion
   in the target. (`RGBA` is already `Hashable`.)
5. **Both render, at their own cells, in their own colours.** One `FrameSnapshot` holding
   `.shot` at `(column: 2, row: 3)`, `.poisonedMushroom(damage: 0)` at `(column: 4, row: 3)`
   and `.mushroom(damage: 0)` at `(column: 6, row: 3)`, rendered at 300×300. Then
   `pixel(x: 25, y: 35) == Palette.shot`, `pixel(x: 45, y: 35) == Palette.poisonedMushroom[0]`,
   `pixel(x: 65, y: 35) == Palette.mushroom[0]`, and the poisoned and plain pixels differ.
   (A 30×30 grid at 300×300 is 10 px per cell; a cell at column `c`, row `r` is sampled at
   `x = c * 10 + 5`, `y = r * 10 + 5`, which is how `OffscreenRendererTests` already
   samples.) Use `try #require(OffscreenRenderer())` and declare the test `throws`.
6. **Nothing that rendered before changed.** `Tests/CentipedeRenderTests/OffscreenRendererTests.swift`
   is unmodified and all of its tests pass, including the four-damage mushroom ramp and the
   distinctness check it already makes. The whole `swift test` suite is green.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Tests/CentipedeRenderTests/OffscreenRendererTests.swift
      - Sources/CentipedeCore/BoardSnapshot.swift
      - Sources/CentipedeCore/World.swift

## Constraints

- No new dependencies, no new targets, no change to `Package.swift`.
- `case mushroom(damage: Int)` keeps its exact shape. Do NOT add an associated value to
  it: `OffscreenRendererTests.swift` is protected and calls `.mushroom(damage: d)` four
  times, so changing that signature fails the build on a file you may not edit.
- Do not touch `OffscreenRenderer`'s vertex builder, shader source or render pass. The
  only edits there are the two palette constants and the two `switch` branches.
- No change to `CentipedeCore`. The core already publishes both facts; this is a render
  slice.

## Risks

- **Adding `poisoned:` to the existing case.** The tempting modelling choice, and it
  breaks the protected renderer tests. The spec's separate case exists to avoid that.
- **A non-exhaustive switch.** `color(for:)` must gain both branches; one alone still
  fails to compile.
- **Index out of range.** `poisonedMushroom[damage]` assumes 0...3, which `World.swift`
  guarantees for real boards. Do not add a clamp — it would diverge from the existing
  `mushroom[damage]` line and is untestable through the adapter.
- **`try` without `throws`.** Criterion 5 uses `try #require`; its test function must be
  declared `throws` or the build fails (DEV-791).
- **Re-emitting more than asked.** `BoardAdapterTests.swift` is given in full above
  because one of its tests must change; every other file is an anchored edit.
