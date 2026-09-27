# Architecture: LLab Axiom Validation & Error Reporting

## Overview
This project replaces fatal process exits on unknown L-System axioms with reportable validation state. Two anchored edits add an error flag, query methods, and a null-root guard to `LSystem`. A new Swift test suite exercises invalid/valid axiom paths and stepping behavior over C++ interop, ensuring existing tests remain untouched while covering all seven approved criteria.

## Components
- **LSystem.h/cpp modifications**: Add `axiomError` member, replace `exit(1)` with state recording, implement `isValid()` / `errorMessage()`, and early-return in `step()` when root is null.
- **AxiomLookupTests.swift (new)**: Swift Testing file declaring `singleSegment()` helper and six parameterized-style tests mapping directly to criteria 1–6. Uses provided bridge (`makeLSystem`, `destroyLSystem`, `addProduction`) exclusively.

## File Structure
```
LLab Shared/Generator/LSystem.h          # modified: adds axiomError, isValid(), errorMessage()
LLab Shared/Generator/LSystem.cpp        # modified: removes exit(1), guards step(), implements queries
Tests/LLabGeneratorTests/AxiomLookupTests.swift # new: swift_test harness for criteria 1-6
```

## Data Models
No new types introduced. Leverages existing `TestLSystem*`, `ProductionMap`, `std.string`, and public `LSystem` members (`age`, `flatModules`). All comparisons use standard library or interop-exposed value semantics.

## Implementation Notes
- Apply anchored edits exactly as quoted; do not reorder declarations or add constructors/destructors.
- `isValid()` returns `root != nullptr`; `errorMessage()` returns `axiomError` by value. Both are `const`.
- `step()` must check `if (root == nullptr) { return; }` before any traversal or flattening logic.
- Swift tests MUST hold plants in `var`, unwrap with `try #require(...)`, defer `destroyLSystem(system)`, wrap C++ strings with `std.string("...")` / `String(...)`, and build maps via `addProduction(&map, ...)`. Never use `.pointee` or subscript assignment on `ProductionMap`.
- No `exit()`, `abort()`, or third-party imports anywhere in the modified/new files.

## Acceptance Criteria Checklist
- [ ] makeLSystem(std.string("(Z,1)"), singleSegment()) returns a plant without exiting; isValid() is false.
- [ ] String(errorMessage()) for that plant contains "(Z,1)".
- [ ] After step(Float(tick)*0.1) for tick in 1...30 on an invalid plant, flatModules.size()==0 and age==0.
- [ ] makeLSystem with empty ProductionMap yields isValid() false; after step(0.5), flatModules.size()==0.
- [ ] With axiom (A,1) and singleSegment(), isValid() is true and String(errorMessage()) is empty.
- [ ] With axiom (A,1) and singleSegment(), after steps at ticks 1..5, age>0 and flatModules.size()>0.
- [ ] Existing LSystemTests.swift unmodified; full swift test suite passes (8 of 8).

## Criterion Seams
- makeLSystem(std.string("(Z,1)"), singleSegment()) returns a plant without exiting; isValid() is false. | setup: `var p = try #require(makeLSystem(std.string("(Z,1)"), singleSegment()))` | act: `p.isValid()` | assert: `#expect(!p.isValid())`
- String(errorMessage()) for that plant contains "(Z,1)". | setup: `var p = try #require(makeLSystem(std.string("(Z,1)"), singleSegment()))` | act: `String(p.errorMessage())` | assert: `#expect(String(p.errorMessage()).contains("(Z,1)"))`
- After step(Float(tick)*0.1) for tick in 1...30 on an invalid plant, flatModules.size()==0 and age==0. | setup: `var p = try #require(makeLSystem(std.string("(Z,1)"), singleSegment())); for t in 1...30 { p.step(Float(t)*0.1) }` | act: `(p.flatModules.size(), p.age)` | assert: `#expect(p.flatModules.size() == 0 && p.age == 0)`
- makeLSystem with empty ProductionMap yields isValid() false; after step(0.5), flatModules.size()==0. | setup: `var p = try #require(makeLSystem(std.string("(A,1)"), ProductionMap())); p.step(0.5)` | act: `(p.isValid(), p.flatModules.size())` | assert: `#expect(!p.isValid() && p.flatModules.size() == 0)`
- With axiom (A,1) and singleSegment(), isValid() is true and String(errorMessage()) is empty. | setup: `var p = try #require(makeLSystem(std.string("(A,1)"), singleSegment()))` | act: `(p.isValid(), String(p.errorMessage()))` | assert: `#expect(p.isValid() && String(p.errorMessage()).isEmpty)`
- With axiom (A,1) and singleSegment(), after steps at ticks 1..5, age>0 and flatModules.size()>0. | setup: `var p = try #require(makeLSystem(std.string("(A,1)"), singleSegment())); for t in 1...5 { p.step(Float(t)*0.1) }` | act: `(p.age, p.flatModules.size())` | assert: `#expect(p.age > 0 && p.flatModules.size() > 0)`
- Existing LSystemTests.swift unmodified; full swift test suite passes (8 of 8). | suite-level
