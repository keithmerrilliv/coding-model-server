# Architecture: LLab Axiom Validation & Safe Stepping

## Overview
Replaces fatal `exit(1)` and null-pointer crashes in `LSystem` with explicit validation state (`isValid`, `errorMessage`). The constructor now captures axiom lookup failures into a private string member, guards `step()` against null roots, and exposes const query methods. Swift tests verify error reporting, safe no-op stepping, and unchanged growth behavior using strict interop harness rules.

## Components
- `LLab Shared/Generator/LSystem.h`: Adds private `string axiomError;` after `createRootModule()`. Replaces final public line with declarations for `bool isValid() const;` and `string errorMessage() const;`.
- `LLab Shared/Generator/LSystem.cpp`: Implements the two query methods. Replaces the debug-only exit block in `createRootModule` with error assignment + conditional cerr output. Inserts early return guard at start of `step(float)`.
- `Tests/LLabGeneratorTests/AxiomLookupTests.swift`: New test file exercising invalid axioms, empty grammars, valid plants, and step semantics under Swift Testing framework constraints.

## File Structure
```
LLab Shared/Generator/LSystem.h          - Modified header (2 anchored edits)
LLab Shared/Generator/LSystem.cpp        - Modified source (2 anchored edits)
Tests/LLabGeneratorTests/AxiomLookupTests.swift - New Swift test suite
```

## Data Models
- `NodeType`: Unchanged C++ enum `{isLeaf, isBranch, noModules}`. No stored state added to it or its containing class.
- `LSystem::axiomError`: Private `std::string`, default-initialized to empty. Populated only when axiom lookup fails.
- `ProductionMap`: Type alias for `std::map<std::string, std::string>` exposed via `LLabTestSupport.h`. Mutated exclusively through `addProduction(&map, key, value)` in tests.

## Implementation Notes
- **Invariant**: If `root == nullptr`, then `flatModules.size() == 0` and `age == 0` regardless of how many times `step()` is invoked. Stepping an invalid plant must be a strict no-op that never dereferences null pointers.
- `isValid()` returns `root != nullptr`. `errorMessage()` returns `axiomError` by value (never reference). Both are marked `const`.
- The constructor signature remains identical; `Renderer.mm` and all read-only files are untouched.
- Tests must declare plants as `var system = try #require(makeLSystem(...))` because inherited non-const members import as mutating over interop. Always pair with `defer { destroyLSystem(system) }`.
- Never use `.pointee`, direct `LSystem` construction, or Swift subscript assignment on `ProductionMap`. Wrap C++ strings with `std.string("...")` and convert results with `String(...)`.

## Acceptance Criteria Checklist
- [ ] C1: makeLSystem(std.string("(Z,1)"), singleSegment()) returns a plant without exiting; isValid() is false.
- [ ] C2: String(errorMessage()) for that plant contains "(Z,1)".
- [ ] C3: After step(Float(tick)*0.1) for tick in 1...30 on an invalid plant, flatModules.size()==0 and age==0.
- [ ] C4: makeLSystem with empty ProductionMap yields isValid() false; after step(0.5), flatModules.size()==0.
- [ ] C5: With axiom (A,1) and singleSegment(), isValid() is true and String(errorMessage()) is empty.
- [ ] C6: With axiom (A,1) and singleSegment(), after steps at ticks 1..5, age>0 and flatModules.size()>0.
- [ ] C7: Existing LSystemTests.swift unmodified; full swift test suite passes (8 of 8).

## Criterion Seams
- C1 | setup: \`var sys = try #require(makeLSystem(std.string("(Z,1)"), singleSegment()))\` | act: \`sys.isValid()\` | assert: \`#expect(!sys.isValid())\`
- C2 | setup: \`var sys = try #require(makeLSystem(std.string("(Z,1)"), singleSegment()))\` | act: \`String(sys.errorMessage())\` | assert: \`#expect(String(sys.errorMessage()).contains("(Z,1"))\`
- C3 | setup: \`var sys = try #require(makeLSystem(std.string("(Z,1)"), singleSegment())); for t in 1...30 { sys.step(Float(t)*0.1) }\` | act: \`(sys.flatModules.size(), sys.age)\` | assert: \`#expect(sys.flatModules.size() == 0 && sys.age == 0)\`
- C4 | setup: \`var sys = try #require(makeLSystem(std.string("(A,1)"), ProductionMap())); sys.step(0.5)\` | act: \`(sys.isValid(), sys.flatModules.size())\` | assert: \`#expect(!sys.isValid() && sys.flatModules.size() == 0)\`
- C5 | setup: \`let sys = try #require(makeLSystem(std.string("(A,1)"), singleSegment()))\` | act: \`(sys.isValid(), String(sys.errorMessage()))\` | assert: \`#expect(sys.isValid() && String(sys.errorMessage()).isEmpty)\`
- C6 | setup: \`var sys = try #require(makeLSystem(std.string("(A,1)"), singleSegment())); for t in 1...5 { sys.step(Float(t)*0.1) }\` | act: \`(sys.age, sys.flatModules.size())\` | assert: \`#expect(sys.age > 0 && sys.flatModules.size() > 0)\`
- C7 suite-level (build/test harness property; no source seam required)
