# Centipede: the frame carries the HUD — score, lives, wave and game-over

Jira: DEV-826, epic DEV-442. Repo `centipede`, `main` = `e09322a` (run 63's delivery,
DEV-825). Every fact below was
read from that commit. A reference implementation of exactly the edits below, with the
tests described, ran on the Mac runner before this spec was written: the suite passed,
with all 6 new tests passing. The new test file alone, against unmodified `main`, fails to
compile (`value of type 'FrameSnapshot' has no member 'hud'`, 12 times;
`cannot find 'HUD' in scope`, 8 times). Do not re-derive the expected values.

## Context

`Sources/CentipedeCore/BoardSnapshot.swift` already carries the game's numbers: a
`BoardSnapshot` has `public var state: GameState`, and `GameState` has public `score`,
`lives` and `wave`. `Sources/CentipedeRender/FrameSnapshot.swift` ends with

```swift
public struct FrameSnapshot: Sendable {
    public var columns: Int
    public var rows: Int
    public var cells: [GridPosition: CellKind]
    public init(columns: Int = 30, rows: Int = 30, cells: [GridPosition: CellKind] = [:]) {
        self.columns = columns
        self.rows = rows
        self.cells = cells
    }
}
```

and `Sources/CentipedeRender/BoardAdapter.swift` builds the frame from the board's
entities, finishing with

```swift
        self.init(columns: board.columns, rows: board.rows, cells: cells)
```

**The defect.** The adapter drops `board.state`, and a frame has nowhere to hold it. The
renderer cannot show the score, the lives left, the wave, or that the game is over.

**The fix.** A `HUD` value holds those four facts, and a frame carries one. The adapter
fills it from `board.state`, and the game is over when `lives <= 0`, the same rule as
`Game.isGameOver`. The frame initialiser gains a trailing `hud:` parameter with a
default, so every existing `FrameSnapshot()` and `FrameSnapshot(columns:rows:cells:)` call
compiles unchanged.

**Concurrency.** Swift tools 6.0, with no default actor isolation. Add no actor
annotations. `HUD` is `Sendable`, like `FrameSnapshot`.

## Required change

### `Sources/CentipedeRender/FrameSnapshot.swift` (modified — one anchored edit)

Replace the whole `public struct FrameSnapshot` declaration, shown verbatim above, with

```swift
/// What the heads-up display shows alongside the board (DEV-826).
public struct HUD: Equatable, Sendable {
    public var score: Int
    public var lives: Int
    public var wave: Int
    public var isGameOver: Bool
    public init(score: Int = 0, lives: Int = 3, wave: Int = 1, isGameOver: Bool = false) {
        self.score = score
        self.lives = lives
        self.wave = wave
        self.isGameOver = isGameOver
    }
}

public struct FrameSnapshot: Sendable {
    public var columns: Int
    public var rows: Int
    public var cells: [GridPosition: CellKind]
    public var hud: HUD
    public init(columns: Int = 30, rows: Int = 30, cells: [GridPosition: CellKind] = [:], hud: HUD = HUD()) {
        self.columns = columns
        self.rows = rows
        self.cells = cells
        self.hud = hud
    }
}
```

`GridPosition` and `CellKind`, above it in the same file, do not change.

### `Sources/CentipedeRender/BoardAdapter.swift` (modified — one anchored edit)

Replace the line `        self.init(columns: board.columns, rows: board.rows, cells: cells)`
with

```swift
        let hud = HUD(score: board.state.score, lives: board.state.lives,
                      wave: board.state.wave, isGameOver: board.state.lives <= 0)
        self.init(columns: board.columns, rows: board.rows, cells: cells, hud: hud)
```

The entity loop above it does not change.

### `Tests/CentipedeRenderTests/HUDFrameTests.swift` (new)

swift-testing, mirroring `BoardAdapterTests.swift`, with plain imports, never
`@testable`:

```swift
import Testing
import CentipedeCore
import CentipedeRender
```

Use `struct HUDFrameTests`, with each test a synchronous `@Test func`. Build boards with
the public `BoardSnapshot(entities:state:)` and `GameState(score:lives:wave:)`
initialisers; no test needs a `Game`. Every test builds its own values.

## Change surface

The two modified files were verified to exist at `main`, and
`Tests/CentipedeRenderTests/HUDFrameTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `Sources/CentipedeRender/FrameSnapshot.swift` | modified (one anchored edit) |
| `Sources/CentipedeRender/BoardAdapter.swift` | modified (one anchored edit) |
| `Tests/CentipedeRenderTests/HUDFrameTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile, and the report must say so.

1. **An empty frame shows the starting HUD.**
   `FrameSnapshot().hud == HUD(score: 0, lives: 3, wave: 1, isGameOver: false)`.
2. **The adapter carries score, lives and wave.** With
   `BoardSnapshot(state: GameState(score: 1234, lives: 2, wave: 3))`,
   `FrameSnapshot(board:).hud == HUD(score: 1234, lives: 2, wave: 3, isGameOver: false)`.
3. **No lives left is game over.** With `GameState(score: 50, lives: 0, wave: 2)`, the
   frame's `hud.isGameOver` is true, `hud.lives == 0` and `hud.score == 50`.
4. **One life left is not game over.** With `GameState(score: 0, lives: 1, wave: 1)`,
   `hud.isGameOver` is false.
5. **The HUD does not disturb the cells.** A board with
   `BoardCell(column: 4, row: 29): .player` and `BoardCell(column: 4, row: 28): .shot`,
   and `GameState(score: 10, lives: 3, wave: 1)`: the frame has exactly 2 cells, `.player`
   at `GridPosition(column: 4, row: 29)`, `.shot` at `GridPosition(column: 4, row: 28)`,
   and `hud.score == 10`.
6. **The existing initialiser still works, and takes a HUD.**
   `FrameSnapshot(columns: 30, rows: 30, cells: [:]).hud == HUD()`. With
   `hud: HUD(score: 7, lives: 1, wave: 4, isGameOver: false)` passed, `hud.score == 7` and
   `hud.wave == 4`.
7. **Existing behaviour intact.** Every existing test file is unmodified, and the whole
   `swift test` suite passes.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Tests/CentipedeRenderTests/BoardAdapterTests.swift
      - Tests/CentipedeRenderTests/OffscreenRendererTests.swift
      - Tests/CentipedeRenderTests/ShotAndPoisonTests.swift
      - Sources/CentipedeRender/OffscreenRenderer.swift
      - Sources/CentipedeCore/BoardSnapshot.swift
      - Sources/CentipedeCore/GameState.swift

## Constraints

- No new dependencies. `CentipedeCore` does not change at all. `GridPosition`,
  `CellKind`, `Palette` and the renderer do not change.
- `hud` is the LAST initialiser parameter and has a default. Existing call sites must not
  change.
- The new test file never uses `@testable import`.

## Risks

- **A required `hud:` parameter.** Without the default, `OffscreenRendererTests.swift`
  and `ShotAndPoisonTests.swift` stop compiling, and they are protected. Criterion 6 pins
  the default.
- **Game-over from the wrong field.** `GameState` has no `isGameOver`. It is
  `lives <= 0`, computed in the adapter. Criteria 3 and 4 pin both sides.
- **Putting `HUD` in the wrong module.** It belongs next to `FrameSnapshot`, in
  `CentipedeRender`, not in `CentipedeCore`.
