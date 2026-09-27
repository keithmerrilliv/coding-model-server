# LLab: a successor that names a module with no production is a reportable error

Jira: DEV-841, epic DEV-449. Repo `llab`, `main` = `fab96bb`. Every fact below was read
from that commit. A reference implementation of exactly the edits below, with the test
file below, ran on the Mac runner before this spec was written:

- With the edits, the whole suite passes: 14 generator tests (the 8 existing and the 6 new
  ones) and 15 store tests.
- The new test file alone, against unmodified `main`, compiles and 5 of its 6 tests fail
  (8 expectations). The sixth, `knownModulesKeepTheirInitialAges`, is the control and
  passes on `main` too.

Do not re-derive the expected values.

## Context

LLab is an Objective-C++/C++ app. Its L-system generator is pure C++ in
`LLab Shared/Generator/` (note the space in `LLab Shared`). `Package.swift` exposes that
directory as the SwiftPM target `LLabGenerator`. The tests are Swift Testing files that call
the C++ directly over Swift's C++ interop, and `swift test` at the repo root runs them.

A production maps a predecessor such as `(A,1)` to a successor string. Inside a successor,
every `(` starts a *timed letter*, `(X,age)`: the character right after the `(` names a
child module, and that module is built from the production whose predecessor starts with
`(X,`. The constructor builds `successorTable`, which maps each such character to its
predecessor. `subdivideModule` looks each child up in that table, and when the lookup
fails it does this:

```cpp
        if (lookUpItr == successorTable.end()) {
            cerr << "Error: no successor table entry for symbol '" << i->first << "'" << endl;
            continue;
        }
```

**The defect.** A successor that names a module with no production (for example `Q` in
`F(Q,0)(B,0.5)`, when there is no `(Q,…)` production) goes wrong in two ways:

- **Silently.** The plant reports `isValid() == true` and an empty `errorMessage()`, and
  the branch is simply missing. DEV-307 added exactly this pair to report grammar errors.
- **With wrong ages.** The `continue` skips the two lines at the bottom of the loop that
  advance the parenthesis positions, so every later module in the same successor reads
  the skipped module's age.

A probe on `main` showed both. With `(B,2) → F`, after 30 steps:

| successor of `(A,1)` | `isValid()` | modules' `initialAge` |
| --- | --- | --- |
| `F(B,0.5)` | true | `[0.0, 0.5]` |
| `F(Q,0)(B,0.5)` | true | `[0.0, 0.0]`: B is born at 0.0, not 0.5 |
| `F(B,0.5)(Q,0)` | true | `[0.0, 0.5]` |

**The fix.** Check the whole grammar when the plant is built. If any successor names a
module with no production, the plant is invalid, just as it is for an unknown axiom:

- `isValid()` is false.
- `errorMessage()` names the production and the module.
- `step()` already does nothing when there is no root.

Every production is checked, whether or not the axiom reaches it. For a valid plant the
`continue` above becomes unreachable, which also retires the age shift. `subdivideModule`
itself does not change.

The existing field `axiomError` holds the message; `errorMessage()` already returns it.

**Concurrency.** Swift tools 6.0, no default actor isolation. The tests are synchronous
free functions; add no actor annotations.

## Required change

### `LLab Shared/Generator/LSystem.h` (modified: one anchored edit)

In the private section, replace these two lines:

```cpp
    void createRootModule();
    string axiomError;
```

with:

```cpp
    void createRootModule();
    string axiomError;    // empty unless the grammar is invalid
    bool checkSuccessors();
```

Nothing else in the header changes.

### `LLab Shared/Generator/LSystem.cpp` (modified: two anchored edits)

**Edit 1.** In `createRootModule`, replace this block:

```cpp
    if(mapItr != this->productions.end()) {
        root = createModule(&(*mapItr), 0.0f, *tortoise);
    } else {
        axiomError = "axiom " + axiom + " matches no production";
        if (display) {
            cerr << "Error: " << axiomError << endl;
        }
    }
```

with:

```cpp
    if (mapItr == this->productions.end()) {
        axiomError = "axiom " + axiom + " matches no production";
    } else if (checkSuccessors()) {
        root = createModule(&(*mapItr), 0.0f, *tortoise);
    }
    if (display && !axiomError.empty()) {
        cerr << "Error: " << axiomError << endl;
    }
```

**Edit 2.** Replace these two lines, which are the start of `isValid` and occur once:

```cpp
bool LSystem::isValid() const
{
```

with:

```cpp
bool LSystem::checkSuccessors()
{
    for (map<string, string>::iterator i = productions.begin(); i != productions.end(); i++) {
        const string& successor = i->second;
        for (size_t pos = successor.find('('); pos != string::npos; pos = successor.find('(', pos + 1)) {
            if (pos + 1 >= successor.length()) {
                break;
            }
            char symbol = successor[pos + 1];
            if (successorTable.find(symbol) == successorTable.end()) {
                axiomError = "successor of " + i->first + " names module " + string(1, symbol)
                    + ", which has no production";
                return false;
            }
        }
    }
    return true;
}

bool LSystem::isValid() const
{
```

The rest of `isValid`, and all of `errorMessage` and `step`, do not change.

### `Tests/LLabGeneratorTests/SuccessorLookupTests.swift` (new)

Write this file exactly. It is the file the reference run used:

```swift
import Testing
import LLabGenerator
import LLabTestSupport
import CxxStdlib

// (A,1) grows into F followed by the given timed letters; (B,2) is a leaf segment.
private func grammar(_ successorOfA: String) -> ProductionMap {
    var productions = ProductionMap()
    addProduction(&productions, std.string("(A,1)"), std.string(successorOfA))
    addProduction(&productions, std.string("(B,2)"), std.string("F"))
    return productions
}

@Test func unknownModuleMakesThePlantInvalid() throws {
    var system = try #require(makeLSystem(std.string("(A,1)"), grammar("F(Q,0)(B,0.5)")))
    defer { destroyLSystem(system) }
    let valid = system.isValid()
    #expect(valid == false)
}

@Test func errorNamesTheProductionAndTheModule() throws {
    var system = try #require(makeLSystem(std.string("(A,1)"), grammar("F(Q,0)(B,0.5)")))
    defer { destroyLSystem(system) }
    let message = String(system.errorMessage())
    #expect(message.contains("(A,1)"))
    #expect(message.contains("module Q"))
}

@Test func steppingAPlantWithAnUnknownModuleDoesNothing() throws {
    var system = try #require(makeLSystem(std.string("(A,1)"), grammar("F(Q,0)(B,0.5)")))
    defer { destroyLSystem(system) }
    for tick in 1...30 { system.step(Float(tick) * 0.1) }
    #expect(system.flatModules.size() == 0)
    #expect(system.age == 0)
}

@Test func anUnknownModuleAfterAKnownOneIsStillInvalid() throws {
    var system = try #require(makeLSystem(std.string("(A,1)"), grammar("F(B,0.5)(Q,0)")))
    defer { destroyLSystem(system) }
    let valid = system.isValid()
    #expect(valid == false)
}

@Test func anUnreachableProductionIsCheckedToo() throws {
    var productions = ProductionMap()
    addProduction(&productions, std.string("(A,1)"), std.string("F"))
    addProduction(&productions, std.string("(C,1)"), std.string("F(Q,0)"))
    var system = try #require(makeLSystem(std.string("(A,1)"), productions))
    defer { destroyLSystem(system) }
    let valid = system.isValid()
    #expect(valid == false)
    #expect(String(system.errorMessage()).contains("(C,1)"))
}

@Test func knownModulesKeepTheirInitialAges() throws {
    var system = try #require(makeLSystem(std.string("(A,1)"), grammar("F(B,0.5)")))
    defer { destroyLSystem(system) }
    let valid = system.isValid()
    #expect(valid == true)
    #expect(String(system.errorMessage()).isEmpty)
    for tick in 1...30 { system.step(Float(tick) * 0.1) }
    let ages = (0..<Int(system.flatModules.size())).map { system.flatModules[$0].initialAge }
    #expect(ages.count == 2)
    #expect(ages.contains(0.5))
}
```

**Why the file looks the way it does.** These are the harness rules from
`Tests/LLabTestSupport/include/LLabTestSupport.h`, which is served read-only. Each exists
because breaking it crashes the tests or fails to link:

- Plants come only from `makeLSystem(axiom, productions)`, which returns a `TestLSystem?`
  (a reference to an `LSystem` subclass). Unwrap it with `try #require(...)` and free it
  with `defer { destroyLSystem(system) }`.
- The plant is held in a `var`, because inherited non-const members such as `step` import
  as `mutating`.
- `isValid()` is read into a `let` before `#expect`, so the macro never copies the plant.
  Never use `.pointee`, and never copy an `LSystem`.
- Grammars are built with `addProduction(&productions, …)`. Never subscript-write a
  `ProductionMap` from Swift: two files that do so fail at link time with a duplicate
  symbol.
- The private helper is named `grammar`, because `LSystemTests.swift` and
  `AxiomLookupTests.swift` already each have a private helper of their own.

## Change surface

The two modified files were verified to exist at `main`, and
`Tests/LLabGeneratorTests/SuccessorLookupTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `LLab Shared/Generator/LSystem.h` | modified (one anchored edit) |
| `LLab Shared/Generator/LSystem.cpp` | modified (two anchored edits) |
| `Tests/LLabGeneratorTests/SuccessorLookupTests.swift` | new |

Each anchor quoted above occurs exactly once in its file at `main`. The protected files
below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file
compiles and 5 of its 6 tests fail, and the report must say which.

1. **An unknown module makes the plant invalid.** With `(A,1) → F(Q,0)(B,0.5)` and
   `(B,2) → F`, `isValid()` is false.
2. **The error names the production and the module.** For that plant, `errorMessage()`
   contains `(A,1)` and `module Q`.
3. **Stepping it does nothing.** After `step(Float(tick) * 0.1)` for `tick` in `1...30`,
   `flatModules.size() == 0` and `age == 0`.
4. **Position does not matter.** With `(A,1) → F(B,0.5)(Q,0)`, `isValid()` is false.
5. **Every production is checked.** With `(A,1) → F` and an unreachable
   `(C,1) → F(Q,0)`, `isValid()` is false and `errorMessage()` contains `(C,1)`.
6. **A valid grammar keeps its ages.** With `(A,1) → F(B,0.5)` and `(B,2) → F`,
   `isValid()` is true, `errorMessage()` is empty, and after 30 steps there are 2 modules,
   one of them with `initialAge` 0.5.
7. **Existing behaviour intact.** `AxiomLookupTests.swift` and `LSystemTests.swift` are
   unmodified. The unknown-axiom message is still `axiom … matches no production`, and the
   whole `swift test` suite passes.

## test_strategy

```yaml
framework: swift_test
required: true
repo: llab
base_ref: main
protected_paths:
  - Package.swift
  - Tests/LLabTestSupport/include/LLabTestSupport.h
  - Tests/LLabTestSupport/LLabTestSupport.cpp
  - Tests/LLabGeneratorTests/LSystemTests.swift
  - Tests/LLabGeneratorTests/AxiomLookupTests.swift
  - LLab.xcodeproj/project.pbxproj
  - LLab Shared/Renderer/Renderer.mm
```

## Constraints

- No `exit()` or `abort()` anywhere in `LLab Shared/Generator/`.
- `subdivideModule`, the constructor's signature, `isValid`, `errorMessage` and `step` do
  not change. `Renderer.mm` does not change.
- `checkSuccessors` is a private member. It is not `const`, because it records the
  error in `axiomError`.
- The test file never uses `.pointee`, never subscript-writes a `ProductionMap`, and never
  calls the `LSystem` constructor directly.

## Risks

- **Fixing `subdivideModule` instead.** Advancing the parenthesis positions on `continue`
  would fix the ages but leave the plant silently wrong and `isValid()` true, so criteria
  1–5 fail. The check belongs where the plant is built.
- **Checking only what the axiom reaches.** Criterion 5 fails. A module can become
  reachable later in growth, and a grammar-wide check is simpler and deterministic.
- **Reporting before the axiom check.** An unknown axiom must still produce the
  `axiom … matches no production` message that `AxiomLookupTests.swift` asserts, so the
  axiom is checked first and `checkSuccessors` runs only when the axiom is found.
- **`#expect(system.isValid() == false)`.** The macro copies its receiver, and the copy
  frees the live tree (SIGSEGV). Read the value into a `let` first, as the file does.
