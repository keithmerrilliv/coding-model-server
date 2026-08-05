# Architecture: Centipede — slice 1 (run 7 design v3, frozen)

## File Structure

```
Sources/CentipedeCore/
├── GameState.swift          (existing — protected)
├── Position.swift           Position struct (col,row); Hashable+Equatable
├── Direction.swift          Direction enum (.left,.right); Equatable
├── SeededRNG.swift          Simple LCG seeded generator; next()->UInt32
├── MushroomCell.swift       damage:Int property; increment/damage methods
├── MushroomField.swift      Grid storage; seed(seed:rng); applyHit(at)->HitResult.MushroomDamage?
├── CentipedeChain.swift     Chain model; advance(field)->newSegments; turn-on-collision logic
├── HitResult.swift          Enum: .mushroom(damage:Int), .head, .bodySplit(frontCount:,rearCount:), .empty
├── GameWorld.swift          Orchestrator; step(); applyHits(at:[Position])->[HitResult]; state snapshot API

Tests/CentipedeCoreTests/
├── GameStateTests.swift     (existing — protected)
├── MushroomFieldTests.swift Criteria 1-3
├── MovementTests.swift      Criteria 4-9
├── SplitOnHitTests.swift    Criteria 8,10-15
└── DeterminismTests.swift   Criterion 16
```


## Data Models

```swift
// Position.swift
struct Position { let col: Int; let row: Int } // Hashable + Equatable

// Direction.swift
enum Direction: Equatable { case left, right }

// SeededRNG.swift
final class SeededRNG { init(seed: UInt32); func next() -> UInt32 }
// LCG: state = (state * 1_664_525 + 1_01_390_422) & 0xFFFF_FFFF

// MushroomCell.swift
struct MushroomCell: Equatable { var damage: Int } // domain 0…3

// HitResult.swift
enum HitResult: Equatable {
    case mushroom(damage: Int)       // new damage level after increment (0→1, 1→2, …)
    case head                        // head destroyed; chain may persist or vanish
    case bodySplit(frontCount: Int, rearCount: Int)
    case empty                       // nothing at cell
}

// CentipedeChain.swift
struct CentipedeChain: Equatable {
    var segments: [Position]         // first element is always the head
    var direction: Direction
}

// GameWorld.swift — public surface
struct GameWorld: Equatable {
    let field: MushroomField          // read-only reference to grid
    var chains: [CentipedeChain]     // ordered list of live chains
    
    // Primary initialiser (seeded)
    init(seed: UInt32)
    
    // Test-visible initialiser for constructing arbitrary state
    init(field: MushroomField, chains: [CentipedeChain])
    
    func step()                       // advance all chains one tick
    func applyHits(at positions: [Position]) -> [HitResult]
    
    // Full-state export for snapshot comparison (criterion 16)
    func snapshot() -> WorldSnapshot
}

struct WorldSnapshot: Equatable {
    let mushrooms: [(position: Position, damage: Int)]   // sorted by position
    let chains: [CentipedeChain]                          // in world order
}
```

