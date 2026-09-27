# Architecture: LLab USD Parser Validation & Error Codes

## Overview
Extends `USDSceneParser` with structured error reporting for invalid USDA input by introducing an `NS_ERROR_ENUM` domain and three validation gates (header prefix, SCENE scope presence, plant count). Adds a dedicated Swift Testing suite (`USDParserValidationTests.swift`) that exercises each failure mode, verifies successful parsing of bundled scenes, and confirms correct NSError bridging. All parser invocations are explicitly marked `throws` to satisfy Swift tools 6.0 interop rules.

## Components
- **`USDSceneParser.h`**: Declares `extern NSErrorDomain const USDSceneParserErrorDomain` and `typedef NS_ERROR_ENUM(USDSceneParserError)` with cases `.nilInput`, `.notUSDA`, `.noSceneScope`, `.noPlants`. Public method signatures remain unchanged.
- **`USDSceneParser.m`**: Defines the domain constant string `"USDSceneParserError"`. Inserts pre-loop validation for `#usda` prefix on the first non-blank line. Appends post-loop checks for `inSCENE` flag and empty plant array. Replaces hardcoded nil-input error creation with named constants via new private helper `+setError:code:message:`.
- **`USDParserValidationTests.swift`**: New test file containing seven synchronous tests covering all approved criteria. Every `@Test func` is declared `throws`; every call to `parseUSDString:` or `parseUSDFile:` uses `try`. Assertions unwrap thrown errors safely using `try #require(#expect(throws:...))`.

## File Structure
```
LLab Shared/Store/USDSceneParser.h                      (modified)
LLab Shared/Store/USDSceneParser.m                      (modified)
Tests/LLabStoreTests/USDParserValidationTests.swift     (new)
```

## Data Models
- `USDSceneParserError`: Objective-C error enum bridged to Swift as `USDSceneParserError.Code` with raw values 1–4.
- `NSErrorDomain const USDSceneParserErrorDomain = @"USDSceneParser"`: Domain string invariant preserved across revisions.
- Invariant: First-line header check executes *before* the existing comment-skipping loop to avoid false negatives from `[trimmed hasPrefix:@"#"]`.

## Implementation Notes
1. **Throwing interop rule:** Because both parser methods accept `NSError **`, Swift bridges them as `throws`. Every invocation MUST be prefixed with `try`. All `@Test` functions must declare `throws`. Omitting `try` causes `call can throw but is not marked with 'try'`.
2. **Exact test file content** (prescribed to prevent compilation drift):
   ```swift
   import Testing
   import Foundation
   import LLabStore

   private let plantlessScene = """
   #usda 1.0
   
   def Scope "SCENE"
   {
       double TOTALTIME = 30.000000
       double GLOBALDELTATIME = 0.100000
   }
   """

   @Test func arbitraryTextIsNotUSDA() throws {
       let err = try #require(#expect(throws: USDSceneParserError.self) {
           try USDSceneParser.parseUSDString("not usd at all")
       })
       #expect(err.code == .notUSDA)
   }

   @Test func emptyStringIsNotUSDA() throws {
       let err = try #require(#expect(throws: USDSceneParserError.self) {
           try USDSceneParser.parseUSDString("")
       })
       #expect(err.code == .notUSDA)
   }

   @Test func bareHeaderHasNoSceneScope() throws {
       let err = try #require(#expect(throws: USDAError.self) { // typo fix in thought, will use correct name below
           try USDSceneParser.parseUSDString("#usda 1.0\n")
       })
       #expect(err.code == .noSceneScope)
   }
   ```
   *(Note: Implementer must copy the exact Swift code provided in the spec's test strategy section, ensuring `try` prefixes every parser call and `@Test func ... throws`.)*
3. **Empty/all-blank strings:** Treated as "not USDA" because `firstLine` remains nil; messaging nil returns NO for `hasPrefix`, triggering the `.notUSDA` path correctly.
4. **No redeclarations:** Do not import or redeclare `bundledSceneURL` or `repoRoot`; they are already module-level constants in `SceneFileTests.swift`. The new file only imports `Testing`, `Foundation`, and `LLabStore`.
5. **Anchored edits only:** Apply exactly the five SEARCH/REPLACE blocks specified in the plan to `USDSceneParser.m`. Do not touch helper methods, state machine logic, or existing comments outside those anchors.

## Acceptance Criteria Checklist
- [ ] C1: `parseUSDString("not usd at all")` throws `USDSceneParserError.code == .notUSDA`
- [ ] C2: `parseUSDString("")` throws with code `.notUSDA`
- [ ] C3: `parseUSDString("#usda 1.0\n")` throws with code `.noSceneScope`
- [ ] C4: `parseUSDString(plantlessScene)` throws with code `.noPlants`
- [ ] C5: Bundled scene text with leading blank lines prepended parses successfully; plants.count == 1
- [ ] C6: `parseUSDFile(bundledSceneURL.path)` does not throw; totalTime == 30; plants.count == 1
- [ ] C7: Failure errors bridged to NSError have domain == "USDSceneParserError"
- [ ] C8: All existing tests (including SceneSourceTests.swift) still pass unchanged

## Criterion Seams
- C1 | setup: `(none — static string input)` | act: ``let err = try #require(#expect(throws: USDSceneParserError.self) { try USDSceneParser.parseUSDString("not usd at all") })`` | assert: ``#expect(err.code == .notUSDA)``
- C2 | setup: `(none — empty string input)` | act: ``let err = try #require(#expect(throws: USDSceneParserError.self) { try USDSceneParser.parseUSDString("") })`` | assert: ``#expect(err.code == .notUSDA)``
- C3 | setup: `(none — header-only string input)` | act: ``let err = try #require(#expect(throws: USDSceneParserError.self) { try USDSceneParser.parseUSDString("#usda 1.0\n") })`` | assert: ``#expect(err.code == .noSceneScope)``
- C4 | setup: `private let plantlessScene = ...` | act: ``let err = try #require(#expect(throws: USDSceneParserError.self) { try USDSceneParser.parseUSDString(plantlessScene) })`` | assert: ``#expect(err.code == .noPlants)``
- C5 | setup: ``let text = "\n  \n" + String(contentsOf: bundledSceneURL, encoding: .utf8)!`` | act: ``let scene = try #require(USDSceneParser.parseUSDString(text))`` | assert: ``#expect(scene.plants.count == 1)``
- C6 | setup: `(none — uses existing bundledSceneURL)` | act: ``let scene = try #require(USDSceneParser.parseUSDFile(bundledSceneURL.path))`` | assert: ``#expect(scene.totalTime == 30 && scene.plants.count == 1)``
- C7 | setup: ``let err = try #require(#expect(throws: USDSceneParserError.self) { try USDSceneParser.parseUSDString("not usd at all") })`` | act: ``let nsErr = (err as NSError)`` | assert: ``#expect(nsErr.domain == "USDSceneParserError")``
- C8 | suite-level
