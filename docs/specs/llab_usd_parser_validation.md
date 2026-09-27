# LLab: the USD parser rejects text that is not a scene, and says why

Jira: DEV-832, epic DEV-449. Repo `llab`, `main` = `c1312a5` (run 67's DEV-308 delivery
plus its test tightening). Every fact below was read from that commit. A reference
implementation of exactly the edits below, with the tests described, ran on the Mac runner
before this spec was written. The `LLabStore` tests passed 15 of 15 (the 8 existing and the
7 new), and the `LLabGenerator` tests passed 8 of 8. The macOS app still builds with the
header change. The new test file alone, against unmodified `main`, fails to compile:
`cannot find 'USDSceneParserError' in scope` (5 times), then
`cannot infer contextual base in reference to member 'notUSDA'` (twice),
`'noSceneScope'` (once) and `'noPlants'` (once). Do not re-derive the expected values.

## Context

`LLab Shared/Store/USDSceneParser.h` and `USDSceneParser.m` are Objective-C (note the
space in `LLab Shared`). SwiftPM builds them into the module `LLabStore`, through symlinks
in `Tests/LLabStoreShim/`, and Swift Testing files in `Tests/LLabStoreTests/` import it.

`+parseUSDString:error:` returns `nullable Scene *` and takes `NSError **`, so Swift
already sees it as throwing: `try USDSceneParser.parseUSDString(text)`. The same goes for
`try USDSceneParser.parseUSDFile(path)`. Its only failure today is a nil string, error
code 1 in the domain `@"USDSceneParserError"`.

**The defect.** The parser never fails on content. `not usd at all`, an empty string, and
`#usda 1.0` with nothing after it all return a `Scene` with zero plants and no error. A
corrupt `scene.usda` therefore reads as a successful load of an empty world, and
`+[Loader loadSceneFromUSD:]` never logs anything, because it only logs when the parser
returns nil. DEV-308's loader skips plantless scenes, but every other caller still gets
the silent success.

**The fix.** Give the existing domain named codes as an `NS_ERROR_ENUM`, and fail with a
reason in three cases:

- **not USDA:** the first non-blank line does not start with `#usda`.
- **no SCENE scope:** the text has no `def Scope "SCENE"`.
- **no plants:** the SCENE scope holds no `PLANT_` scope.

The domain string stays `@"USDSceneParserError"`.

**Concurrency.** Swift tools 6.0, no default actor isolation. The tests are synchronous
free functions; add no actor annotations.

## Required change

### `LLab Shared/Store/USDSceneParser.h` (modified: one anchored edit)

Replace the line `@interface USDSceneParser : NSObject` with

```objc
/// Error domain for USDSceneParser failures (DEV-832).
extern NSErrorDomain const USDSceneParserErrorDomain;

/// Why a USD string could not be parsed into a scene (DEV-832).
typedef NS_ERROR_ENUM(USDSceneParserErrorDomain, USDSceneParserError) {
    USDSceneParserErrorNilInput = 1,
    USDSceneParserErrorNotUSDA = 2,
    USDSceneParserErrorNoSceneScope = 3,
    USDSceneParserErrorNoPlants = 4,
};

@interface USDSceneParser : NSObject
```

Swift sees this as the error type `USDSceneParserError`, whose `code` is a
`USDSceneParserError.Code` with the cases `.nilInput`, `.notUSDA`, `.noSceneScope` and
`.noPlants`.

### `LLab Shared/Store/USDSceneParser.m` (modified: five anchored edits)

1. Replace the line `@implementation USDSceneParser` with

   ```objc
   NSErrorDomain const USDSceneParserErrorDomain = @"USDSceneParserError";

   @implementation USDSceneParser
   ```

2. In the nil-string branch of `parseUSDString:error:`, replace the two lines

   ```objc
               *error = [NSError errorWithDomain:@"USDSceneParserError"
                                            code:1
   ```

   with

   ```objc
               *error = [NSError errorWithDomain:USDSceneParserErrorDomain
                                            code:USDSceneParserErrorNilInput
   ```

3. Replace the line
   `    NSArray<NSString *> *lines = [usdString componentsSeparatedByString:@"\n"];` with

   ```objc
       NSArray<NSString *> *lines = [usdString componentsSeparatedByString:@"\n"];

       NSString *firstLine = nil;
       for (NSString *line in lines) {
           NSString *trimmed = [line stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
           if (trimmed.length > 0) {
               firstLine = trimmed;
               break;
           }
       }
       if (![firstLine hasPrefix:@"#usda"]) {
           [self setError:error code:USDSceneParserErrorNotUSDA
                  message:@"not a USDA text file: the first line must start with #usda"];
           return nil;
       }
   ```

   `firstLine` is nil for an empty or all-blank string, and messaging nil returns `NO`, so
   that case is "not USDA" too.

4. Replace the line `    scene.plants = [plants copy];` near the end of
   `parseUSDString:error:` with

   ```objc
       if (!inSCENE) {
           [self setError:error code:USDSceneParserErrorNoSceneScope
                  message:@"no def Scope \"SCENE\" in the file"];
           return nil;
       }
       if (plants.count == 0) {
           [self setError:error code:USDSceneParserErrorNoPlants
                  message:@"the SCENE scope holds no PLANT_ scope"];
           return nil;
       }

       scene.plants = [plants copy];
   ```

   `inSCENE` is the existing local, set to `YES` when the parser meets
   `def Scope "SCENE"` and never reset.

5. Replace the line `#pragma mark - Helper Methods` with

   ```objc
   #pragma mark - Helper Methods

   + (void)setError:(NSError **)error code:(USDSceneParserError)code message:(NSString *)message {
       if (error) {
           *error = [NSError errorWithDomain:USDSceneParserErrorDomain
                                        code:code
                                    userInfo:@{NSLocalizedDescriptionKey: message}];
       }
   }
   ```

   `setError:code:message:` stays private: do not declare it in the header.

Nothing else in the parser changes. The state machine, `extractDoubleValue:` and
`extractStringValue:` stay as they are.

### `Tests/LLabStoreTests/USDParserValidationTests.swift` (new)

A Swift Testing file with exactly these imports:

```swift
import Testing
import Foundation
import LLabStore
```

The read-only `Tests/LLabStoreTests/SceneFileTests.swift` already declares the module-level
`bundledSceneURL` (the URL of `LLab Shared/scene.usda`). Use it, and **do not redeclare
it**, nor `repoRoot`.

Check a failure with Swift Testing's error-returning form, which yields the typed error:

```swift
let error = #expect(throws: USDSceneParserError.self) {
    try USDSceneParser.parseUSDString("not usd at all")
}
#expect(error?.code == .notUSDA)
```

Give the file one private constant, `plantlessScene`, a multi-line string holding exactly:

```
#usda 1.0

def Scope "SCENE"
{
    double TOTALTIME = 30.000000
    double GLOBALDELTATIME = 0.100000
}
```

## Change surface

The two modified files were verified to exist at `main`, and
`Tests/LLabStoreTests/USDParserValidationTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `LLab Shared/Store/USDSceneParser.h` | modified (one anchored edit) |
| `LLab Shared/Store/USDSceneParser.m` | modified (five anchored edits) |
| `Tests/LLabStoreTests/USDParserValidationTests.swift` | new |

Each anchor quoted above occurs exactly once in its file at `main`. The files under
`Tests/LLabStoreShim/` are symlinks to the real Store sources; edit `LLab Shared/Store/`,
never the links, and never output a file under `Tests/LLabStoreShim/`. The protected files
below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile, and the report must say so.

1. **Arbitrary text is not USDA.** `parseUSDString("not usd at all")` throws a
   `USDSceneParserError` whose `code` is `.notUSDA`.
2. **An empty string is not USDA.** `parseUSDString("")` throws with code `.notUSDA`.
3. **A bare header has no SCENE scope.** `parseUSDString("#usda 1.0\n")` throws with code
   `.noSceneScope`.
4. **A SCENE without plants has no plants.** `parseUSDString(plantlessScene)` throws with
   code `.noPlants`.
5. **Leading blank lines before the header are allowed.** The bundled scene's text (read
   from `bundledSceneURL`) with `"\n  \n"` prepended parses without throwing to
   `plants.count == 1`.
6. **The bundled scene file still parses.** `parseUSDFile(bundledSceneURL.path)` does not
   throw, `totalTime == 30` and `plants.count == 1`.
7. **Errors keep the parser's domain.** For criterion 1's failure, the error bridged to
   `NSError` has `domain == "USDSceneParserError"`.
8. **Existing behaviour intact.** Every other test file is unmodified, and the whole
   `swift test` suite passes. That includes DEV-308's `SceneSourceTests.swift`, whose
   plantless-scene tests now take the parser's error path.

## test_strategy

- framework: swift_test
- required: true
- repo: llab
- base_ref: main
- protected_paths:
  - Package.swift
  - LLab.xcodeproj/project.pbxproj
  - LLab Shared/scene.usda
  - LLab Shared/Store/Loader.h
  - LLab Shared/Store/Loader.m
  - LLab Shared/Store/SceneParser.m
  - LLab Shared/Model/SceneModels.h
  - LLab Shared/Model/SceneModels.m
  - Tests/LLabStoreTests/SceneFileTests.swift
  - Tests/LLabStoreTests/SceneSourceTests.swift
  - Tests/LLabStoreShim/README.md
  - Tests/LLabGeneratorTests/LSystemTests.swift
  - Tests/LLabGeneratorTests/AxiomLookupTests.swift
  - Tests/LLabTestSupport/include/LLabTestSupport.h
  - Tests/LLabTestSupport/LLabTestSupport.cpp

## Constraints

- The domain string stays `@"USDSceneParserError"`, and code 1 keeps meaning a nil string.
- The parser's public method signatures do not change. `Loader` is protected and does not
  change.
- No new files outside the three in the change surface.
- The new test file never uses `@testable import`, and never redeclares `bundledSceneURL`
  or `repoRoot`.

## Risks

- **Checking the header after the comment skip.** The parse loop skips every line starting
  with `#` as a comment, so a header check inside the loop never sees `#usda`. The check
  runs before the loop, on the first non-blank line.
- **A plain `NSInteger` enum instead of `NS_ERROR_ENUM`.** Swift would then see the
  failure as a bare `NSError`, and `#expect(throws: USDSceneParserError.self)` would not
  compile. The error enum is what gives Swift the typed error.
- **Renaming the domain.** Anything that matched the old string would break, and
  criterion 7 fails.
- **Redeclaring `bundledSceneURL`.** `SceneFileTests.swift` declares it in the same module;
  a second declaration is an "invalid redeclaration" compile error.
