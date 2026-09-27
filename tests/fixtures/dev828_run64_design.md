# Architecture: Centipede HUD Integration (DEV-826)

## Overview
Add a `HUD` value type carrying score, lives, wave, and isGameOver to `FrameSnapshot`, so the renderer can display heads-up information. The adapter derives it from `BoardSnapshot.state`, computing `isGameOver` as `lives <= 0`. All types remain structs with mutable stored properties; no classes are introduced.

## Components
- **HUD** — Equatable/Sendable struct in FrameSnapshot.swift holding four public vars (score, lives, wave, isGameOver). Default init provides starting values.
- **FrameSnapshot.hud** — New mutable stored property on the existing struct. Initialiser gains trailing `hud:` parameter with default `HUD()`.
- **BoardAdapter extension** — Computes HUD from board state before delegating to self.init. Entity loop unchanged.
- **HUDFrameTests** — New test file exercising all seven acceptance criteria via BoardSnapshot construction and direct property observation.

## File Structure
```
Sources/CentipedeRender/FrameSnapshot.swift   — modified: add HUD struct, add hud var + param to FrameSnapshot
Sources/CentipedeRender/BoardAdapter.swift    — modified: compute HUD from board.state before self.init call
Tests/CentipedeRenderTests/HUDFrameTests.swift — new: swift_test suite for all approved criteria
```

## Data Models
```swift
public struct HUD: Equatable, Sendable {
    public var score: Int
    public var lives: Int
    public var wave: Int
    public var isGameOver: Bool
}

// FrameSnapshot remains a struct; ALL stored properties are mutable vars:
public struct FrameSnapshot: Sendable {
    public var columns: Int          // already mutable in baseline
    public var rows: Int             // already mutable in baseline
    public var cells: [GridPosition: CellKind]  // already mutable in baseline
    public var hud: HUD              // NEW, same mutability pattern as siblings
}
```

### Invariant (rule 8)
`isGameOver == (lives <= 0)` whenever the adapter constructs it from `GameState`. The HUD itself does not enforce this invariant — it is a plain data carrier. Only the BoardAdapter must satisfy it at construction time.

## Implementation Notes
1. **Mutability contract**: `HUD` and `FrameSnapshot` remain structs with `var` stored properties. Test code that needs to mutate cells binds `var snap = ...`, matching existing OffscreenRendererTests.swift patterns. No member functions need `mutating` because we only store values, never transform them in-place.
2. **hud parameter position**: Must be LAST in the init signature so every existing call site (`FrameSnapshot()`, `FrameSnapshot(columns:rows:cells:)`) compiles unchanged without argument labels.
3. **Default value**: `hud: HUD()` uses the default initialiser producing score=0, lives=3, wave=1, isGameOver=false. This matches criterion C1 exactly.
4. **Game-over derivation**: Computed in the adapter as `board.state.lives <= 0`, NOT read from GameState (which has no such field). Criteria C3/C4 pin both sides of the boundary.
5. **No @testable import** in the new test file; all accessed members are public.
6. **Equatable conformance on HUD**: Required so criteria can assert `snap.hud == HUD(...)` directly. Synthesised by compiler since all four fields are Equatable.

## Acceptance Criteria Checklist
- [ ] C1: An empty frame shows the starting HUD — `FrameSnapshot().hud == HUD(score: 0, lives: 3, wave: 1, isGameOver: false)`.
- [ ] C2: The adapter carries score, lives and wave — BoardSnapshot with state GameState(1234,2,3) produces hud matching those values and isGameOver==false.
- [ ] C3: Zero lives triggers game over — GameState(50,0,2) yields hud.isGameOver==true while preserving hud.lives==0 and hud.score==50.
- [ ] C4: One life remaining keeps hud.isGameOver false — GameState(0,1,1) yields hud.isGameOver==false.
- [ ] C5: Adding HUD state does not disturb cell mapping — board with .player at (4,29) and .shot at (4,28), state(10,3,1) → exactly two cells present at expected positions AND hud.score==10.
- [ ] C6a: Existing initialiser still works — FrameSnapshot(columns:30,rows:30,cells:[]).hud equals default HUD().
- [ ] C6b: Explicit hud parameter propagates correctly through initialiser — passing hud:HUD(score:7,lives:1,wave:4,isGameOver:false) yields hud.score==7 and hud.wave==4.
- [ ] C7: Existing behaviour intact — every existing test file unmodified; full swift test suite passes.

## Criterion Seams
- C1 | setup: `let snap = FrameSnapshot()` | act: `snap.hud` | assert: `snap.hud == HUD(score: 0, lives: 3, wave: 1, isGameOver: false)`
- C2 | setup: `let board = BoardSnapshot(state: GameState(score: 1234, lives: 2, wave: 3))` | act: `FrameSnapshot(board: board).hud` | assert: `frame.hud == HUD(score: 1234, lives: 2, wave: 3, isGameOver: false)` where `frame = FrameSnapshot(board: board)`
- C3 | setup: `let board = BoardSnapshot(state: GameState(score: 50, lives: 0, wave: 2))` | act: `FrameSnapshot(board: board).hud` | assert: `frame.hud.isGameOver && frame.hud.lives == 0 && frame.hud.score == 50` where `frame = FrameSnapshot(board: board)`
- C4 | setup: `let board = BoardSnapshot(state: GameState(score: 0, lives: 1, wave: 1))` | act: `FrameSnapshot(board: board).hud` | assert: `!frame.hud.isGameOver` where `frame = FrameSnapshot(board: board)`
- C5 | setup: `let board = BoardSnapshot(entities: [BoardCell(column: 4, row: 29): .player, BoardCell(column: 4, row: 28): .shot], state: GameState(score: 10, lives: 3, wave: 1))` | act: `FrameSnapshot(board: board)` | assert: `frame.cells.count == 2 && frame.cells[GridPosition(column: 4, row: 29)] == .player && frame.cells[GridPosition(column: 4, row: 28)] == .shot && frame.hud.score == 10` where `frame = FrameSnapshot(board: board)`
- C6a | setup: `let snap = FrameSnapshot(columns: 30, rows: 30, cells: [:])` | act: `snap.hud` | assert: `snap.hud == HUD()`
- C6b | setup: `let snap = FrameSnapshot(hud: HUD(score: 7, lives: 1, wave: 4, isGameOver: false))` | act: `snap.hud` | assert: `snap.hud.score == 7 && snap.hud.wave == 4`
- C7 | suite-level — build and test outcome observed by the runner, not by a source call.
