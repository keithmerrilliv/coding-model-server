# LLab: an unknown axiom is a reportable error, not a process exit

Jira: DEV-307, epic DEV-449. Repo `llab`, `main` = `30bd8c8` (the DEV-830 test harness).
Every fact below was read from that commit. A reference implementation of exactly the
edits below, with the tests described, ran on the Mac runner before this spec was
written: the suite passed 8 of 8 (the 2 existing tests and the 6 new ones). The new test
file alone, against unmodified `main`, fails to compile
(`value of type 'TestLSystem' has no member 'isValid'`, 6 times;
`value of type 'TestLSystem' has no member 'errorMessage'`, 4 times). Do not re-derive
the expected values.

## Context

LLab is an Objective-C++/C++ app. Its L-system generator is pure C++ in
`LLab Shared/Generator/` (note the space in `LLab Shared`). `Package.swift` exposes that
directory as the SwiftPM target `LLabGenerator`, and tests are Swift Testing files that call
the C++ directly over Swift's C++ interop. `swift test` at the repo root runs them.

`LSystem`'s constructor ends by calling `createRootModule()`, in
`LLab Shared/Generator/LSystem.cpp`. That function looks the axiom up in the production map
and ends with

```cpp
    if(mapItr != this->productions.end()) {
        root = createModule(&(*mapItr), 0.0f, *tortoise);
    } else if (display) {
        cerr << "Error: axiom string incorrect in L-System constructor.\n";
        exit(1);
    }
```

`display` is the constructor's last argument, `debug`, and the app always passes `true`.

**The defect.** When the axiom matches no production:

- With `debug` true, the constructor calls `exit(1)` and kills the whole app. A typo in a
  scene file quits the program, and the message does not say which axiom was wrong.
- With `debug` false, `root` stays `nullptr`, and the first `step()` calls
  `traverseDTree(root)` on it and crashes.

Either way the caller cannot find out that the plant is invalid.

**The fix.**

- `createRootModule` records why the plant is invalid and never exits. It still writes
  the message to `cerr` when `display` is on.
- Two new public members report the state: `isValid()` and `errorMessage()`.
- `step()` does nothing on an invalid plant, so it renders nothing and never crashes.

**Concurrency.** Swift tools 6.0, no default actor isolation. The tests are synchronous
free functions; add no actor annotations.

## Required change

### `LLab Shared/Generator/LSystem.h` (modified: two anchored edits)

1. Replace the line `    void createRootModule();` (it is in the private section) with

   ```cpp
       void createRootModule();
       string axiomError;    // empty unless the axiom matches no production
   ```

2. Replace the line `    void step(float currentGlobalTime);` (the last public member) with

   ```cpp
       void step(float currentGlobalTime);
       bool isValid() const;
       string errorMessage() const;
   ```

Nothing else in the header changes. Do not add copy or move constructors.

### `LLab Shared/Generator/LSystem.cpp` (modified: two anchored edits)

1. In `createRootModule`, replace

   ```cpp
       } else if (display) {
           cerr << "Error: axiom string incorrect in L-System constructor.\n";
           exit(1);
       }
   ```

   with

   ```cpp
       } else {
           axiomError = "axiom " + axiom + " matches no production";
           if (display) {
               cerr << "Error: " << axiomError << endl;
           }
       }
   ```

2. Replace the two lines

   ```cpp
   void LSystem::step(float currentGlobalTime)
   {
   ```

   with

   ```cpp
   bool LSystem::isValid() const
   {
       return root != nullptr;
   }

   string LSystem::errorMessage() const
   {
       return axiomError;
   }

   void LSystem::step(float currentGlobalTime)
   {
       if (root == nullptr) {
           return;
       }
   ```

   The rest of `step` does not change.

### `Tests/LLabGeneratorTests/AxiomLookupTests.swift` (new)

A Swift Testing file, with exactly these imports:

```swift
import Testing
import LLabGenerator
import LLabTestSupport
import CxxStdlib
```

**The harness rules.** `Tests/LLabTestSupport/include/LLabTestSupport.h` is served
read-only, and each rule below exists because breaking it crashes or fails to link:

- **Create plants only with `makeLSystem(axiom, productions)`.** It returns a
  `TestLSystem?` (a reference to an `LSystem` subclass), so unwrap it with
  `try #require(...)`. Each test function is therefore `throws`.
- **Free every plant.** Put `defer { destroyLSystem(system) }` on the line after it is
  created.
- **Hold the plant in a `var`.** Inherited non-const members such as `step` import as
  `mutating`. An unneeded `var` only warns.
- **Call members directly:** `system.isValid()`, `system.step(0.5)`, `system.age`,
  `system.flatModules.size()`. Never use `.pointee`, and never copy an `LSystem`.
- **Build grammars with `ProductionMap()` and
  `addProduction(&productions, std.string("(A,1)"), std.string("F"))`.** Never write
  `productions[key] = value` from Swift: two test files that do so fail to link.
- **Wrap strings:** pass them as `std.string("…")`, and convert a returned `std.string`
  with `String(...)`.

Define one private helper in this file, `singleSegment()`, which returns a `ProductionMap`
with the single production `(A,1)` → `F`. `LSystemTests.swift` already has a private
helper of its own, so use this name, not `embeddedPlant`. Every test builds its own plant.

## Change surface

The two modified files were verified to exist at `main`, and
`Tests/LLabGeneratorTests/AxiomLookupTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `LLab Shared/Generator/LSystem.h` | modified (two anchored edits) |
| `LLab Shared/Generator/LSystem.cpp` | modified (two anchored edits) |
| `Tests/LLabGeneratorTests/AxiomLookupTests.swift` | new |

Each anchor quoted above occurs exactly once in its file at `main`. The protected files
below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile, and the report must say so.

1. **An unknown axiom is reported, not fatal.** `makeLSystem(std.string("(Z,1)"),
   singleSegment())` returns a plant (the process does not exit), and `isValid()` is
   false.
2. **The error names the axiom.** For that plant, `String(errorMessage())` contains
   `(Z,1)`.
3. **Stepping an invalid plant is a no-op.** After `step(Float(tick) * 0.1)` for
   `tick` in `1...30`, `flatModules.size() == 0` and `age == 0`.
4. **An empty grammar is invalid.** `makeLSystem(std.string("(A,1)"), ProductionMap())`
   has `isValid()` false, and after `step(0.5)` has `flatModules.size() == 0`.
5. **A known axiom is valid with no error.** With `(A,1)` and `singleSegment()`,
   `isValid()` is true and `String(errorMessage())` is empty.
6. **A known axiom still grows.** With `(A,1)` and `singleSegment()`, after
   `step(Float(tick) * 0.1)` for `tick` in `1...5`, `age > 0` and
   `flatModules.size() > 0`.
7. **Existing behaviour intact.** `LSystemTests.swift` is unmodified, and the whole
   `swift test` suite passes.

## test_strategy

- framework: swift_test
- required: true
- repo: llab
- base_ref: main
- protected_paths:
  - Package.swift
  - Tests/LLabTestSupport/include/LLabTestSupport.h
  - Tests/LLabTestSupport/LLabTestSupport.cpp
  - Tests/LLabGeneratorTests/LSystemTests.swift
  - LLab.xcodeproj/project.pbxproj
  - LLab Shared/Renderer/Renderer.mm

## Constraints

- No `exit()` or `abort()` anywhere in `LLab Shared/Generator/`.
- `isValid()` and `errorMessage()` are `const` members, and `errorMessage()` returns
  `string` by value, not by reference.
- The constructor's signature does not change, and `Renderer.mm` does not change.
- The test file never uses `.pointee`, never subscript-writes a `ProductionMap`, and never
  calls the `LSystem` constructor directly.

## Risks

- **Guarding only the `display` branch.** The `debug`-false path crashed in `step()`, not
  in the constructor. Criteria 3 and 4 fail with a crash unless `step` returns early on a
  null `root`.
- **Returning `const string&`.** Swift imports C++ methods that return references
  unsafely. Return by value, as the reference does.
- **Tests that copy the plant.** `#expect(system.pointee.isValid())` copies the
  `LSystem`, and the copy frees the live tree when it is destroyed (SIGSEGV). The
  `TestLSystem` reference exists to prevent this; use it as the harness rules say.
- **Subscript-writing a map.** Every Swift file that writes `productions[key] = value`
  synthesizes the same `CxxPair` witness, so the moment two files do it the suite fails at
  LINK time with a duplicate symbol. That is a trap for whichever test file is written
  next. Use `addProduction`.
