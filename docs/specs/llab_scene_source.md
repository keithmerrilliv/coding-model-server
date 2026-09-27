# LLab: the app loads its bundled scene, and says where every scene came from

Jira: DEV-308, epic DEV-449. Repo `llab`, `main` = `c1a8354`: `caafa41` bundles
`scene.usda`, `97c8cf9` adds the `LLabStore` test target, and `c1a8354` is run 66's
DEV-307 delivery. Every fact below was read from that commit. A reference implementation
of exactly the edits below, with the tests described, ran on the Mac Studio before this
spec was written. The `LLabStore` tests passed 8 of 8 (the existing one and the 7 new
ones), and the `LLabGenerator` tests passed 8 of 8.
The new test file alone, against unmodified `main`, fails to compile:
`cannot find 'LLabSceneSource' in scope` (8 times),
`argument passed to call that takes no arguments` (8 times) and
`'nil' requires a contextual type` (5 times). Do not re-derive the expected values.

## Context

LLab's scene loader is Objective-C: `LLab Shared/Store/Loader.h` and `Loader.m` (note the
space in `LLab Shared`). SwiftPM builds the Store sources as the module `LLabStore`, through
symlinks in `Tests/LLabStoreShim/`, and Swift Testing files in `Tests/LLabStoreTests/`
import it. `swift test` at the repo root runs them.

`caafa41` moved `scene.usda` to `LLab Shared/scene.usda` and added it to both app targets.
It is now copied into the app bundle's resources. But `+[Loader loadScene]` only ever
looks in the Documents directory:

```objc
    NSString *documentsPath = [NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES) firstObject];
    NSString *usdPath = [documentsPath stringByAppendingPathComponent:defaultUSDFileName];
```

**The defects.**

1. **The bundled scene is never read.** A fresh install has nothing in Documents, so it
   always falls back to the hard-coded JSON scene.
2. **A `scene.usda` that yields no plants counts as a success.** `USDSceneParser` returns a
   scene with **zero plants**, not nil, for a file that is not USD at all, is empty, or
   holds only the `#usda 1.0` header. So a corrupt Documents file renders an empty world
   and never falls back.
3. **The caller cannot tell which source was used.** `loadScene` returns only the scene.

**The fix.** A new class method takes both directories and reports where the scene came
from. It tries, in order:

1. `<documents>/scene.usda`
2. `<resources>/scene.usda`
3. the embedded JSON

A `scene.usda` that is missing, fails to parse, or holds no plants is skipped. Falling back
to the embedded JSON logs a warning that names both directories. `loadScene` keeps its
signature and becomes a one-line call with the real Documents directory and
`[[NSBundle mainBundle] resourcePath]`.

**Concurrency.** Swift tools 6.0, no default actor isolation. The tests are synchronous
free functions; add no actor annotations.

## Required change

### `LLab Shared/Store/Loader.h` (modified: two anchored edits)

1. Replace the line `@interface Loader : NSObject` with

   ```objc
   /// Where a loaded scene came from (DEV-308).
   typedef NS_ENUM(NSInteger, LLabSceneSource) {
       LLabSceneSourceUnknown = 0,
       LLabSceneSourceDocuments,
       LLabSceneSourceBundle,
       LLabSceneSourceEmbeddedJSON,
   };

   @interface Loader : NSObject
   ```

2. Replace the line `+ (nullable Scene *)loadScene;` with

   ```objc
   + (nullable Scene *)loadScene;

   /**
    * Load the scene from the first usable source: <documentsDirectory>/scene.usda,
    * then <resourceDirectory>/scene.usda, then the embedded JSON. A scene.usda that
    * is missing, fails to parse, or holds no plants is skipped.
    * @param source Set to where the scene came from; LLabSceneSourceUnknown on nil
    */
   + (nullable Scene *)loadSceneFromDocuments:(nullable NSString *)documentsDirectory
                                    resources:(nullable NSString *)resourceDirectory
                                       source:(nullable LLabSceneSource *)source;
   ```

Swift sees these as `LLabSceneSource` with the cases `.unknown`, `.documents`, `.bundle`
and `.embeddedJSON`, and as `Loader.loadScene(fromDocuments:resources:source:)`, where
`source` takes `&someVar`.

### `LLab Shared/Store/Loader.m` (modified: one anchored edit)

Replace the whole existing `loadScene` method, from `+ (nullable Scene *)loadScene {` to
its closing `}` and ending just before `+ (nullable Scene *)loadSceneFromUSD:`. It is
quoted here verbatim:

```objc
+ (nullable Scene *)loadScene {
    NSError *error = nil;

    // Try to load from USD first (in Documents directory)
    NSString *documentsPath = [NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES) firstObject];
    NSString *usdPath = [documentsPath stringByAppendingPathComponent:defaultUSDFileName];

    if ([[NSFileManager defaultManager] fileExistsAtPath:usdPath]) {
        NSLog(@"Loading scene from USD: %@", usdPath);
        Scene *scene = [self loadSceneFromUSD:usdPath];
        if (scene) {
            NSLog(@"✓ Successfully loaded scene from USD!");
            [self logSceneInfo:scene];
            return scene;
        }
        NSLog(@"⚠ Failed to load USD, falling back to embedded JSON");
    } else {
        NSLog(@"USD file not found at %@", usdPath);
        NSLog(@"Falling back to embedded JSON");
    }

    // Fall back to JSON
    NSLog(@"Parsing embedded JSON...");
    Scene *scene = [SceneParser parseJSONString:jsonString error:&error];

    if (!scene) {
        NSLog(@"Error parsing JSON: %@", error.localizedDescription);
        return nil;
    }

    NSLog(@"✓ Successfully parsed JSON!");
    [self logSceneInfo:scene];
    return scene;
}
```

Replace it with:

```objc
+ (nullable Scene *)loadScene {
    NSString *documentsPath = [NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES) firstObject];
    return [self loadSceneFromDocuments:documentsPath
                              resources:[[NSBundle mainBundle] resourcePath]
                                 source:NULL];
}

+ (nullable Scene *)loadSceneFromDocuments:(nullable NSString *)documentsDirectory
                                 resources:(nullable NSString *)resourceDirectory
                                    source:(nullable LLabSceneSource *)source {
    if (source) {
        *source = LLabSceneSourceUnknown;
    }

    Scene *scene = [self usableSceneInDirectory:documentsDirectory];
    if (scene) {
        if (source) {
            *source = LLabSceneSourceDocuments;
        }
        return scene;
    }

    scene = [self usableSceneInDirectory:resourceDirectory];
    if (scene) {
        if (source) {
            *source = LLabSceneSourceBundle;
        }
        return scene;
    }

    NSLog(@"⚠ No usable %@ in Documents (%@) or the app bundle (%@); using the embedded JSON scene",
          defaultUSDFileName, documentsDirectory ?: @"none", resourceDirectory ?: @"none");
    NSError *error = nil;
    scene = [SceneParser parseJSONString:jsonString error:&error];
    if (!scene) {
        NSLog(@"Error parsing JSON: %@", error.localizedDescription);
        return nil;
    }
    if (source) {
        *source = LLabSceneSourceEmbeddedJSON;
    }
    [self logSceneInfo:scene];
    return scene;
}

+ (nullable Scene *)usableSceneInDirectory:(nullable NSString *)directory {
    if (directory.length == 0) {
        return nil;
    }
    NSString *path = [directory stringByAppendingPathComponent:defaultUSDFileName];
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        return nil;
    }
    Scene *scene = [self loadSceneFromUSD:path];
    if (scene.plants.count == 0) {
        NSLog(@"⚠ %@ holds no plants; skipping it", path);
        return nil;
    }
    NSLog(@"✓ Loaded scene from USD: %@", path);
    [self logSceneInfo:scene];
    return scene;
}
```

`loadSceneFromUSD:`, `exportEmbeddedSceneToUSD:`, `logSceneInfo:` and the embedded
`jsonString` do not change. `usableSceneInDirectory:` stays private: do not declare it in
the header.

### `Tests/LLabStoreTests/SceneSourceTests.swift` (new)

A Swift Testing file with exactly these imports:

```swift
import Testing
import Foundation
import LLabStore
```

The existing `Tests/LLabStoreTests/SceneFileTests.swift` (read-only) already declares two
module-level constants: `repoRoot`, the repository root URL, and `bundledSceneURL`, the
URL of `LLab Shared/scene.usda`. Use `bundledSceneURL` in this file, and **do not
redeclare either one**: they share a module, and a second declaration will not compile.

Give this file three private helpers:

- `makeDirectory() throws -> URL`: creates and returns a fresh empty directory under
  `FileManager.default.temporaryDirectory`, named with a `UUID`, so parallel tests never
  share one.
- `placeBundledScene(in: URL) throws`: copies `bundledSceneURL` into the directory as
  `scene.usda`.
- `placePlantlessScene(in: URL) throws`: writes the text `not usd at all` into the
  directory as `scene.usda`.

Call the loader as `var source = LLabSceneSource.unknown` followed by
`try #require(Loader.loadScene(fromDocuments: …, resources: …, source: &source))`. Each
test function is `throws`, and every test builds its own directories.

## Change surface

The two modified files were verified to exist at `main`, and
`Tests/LLabStoreTests/SceneSourceTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `LLab Shared/Store/Loader.h` | modified (two anchored edits) |
| `LLab Shared/Store/Loader.m` | modified (one anchored edit: the whole `loadScene` method) |
| `Tests/LLabStoreTests/SceneSourceTests.swift` | new |

Each anchor quoted above occurs exactly once in its file at `main`. The files under
`Tests/LLabStoreShim/` are symlinks to the real Store sources; edit `LLab Shared/Store/`,
never the links. The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile, and the report must say so. In every criterion, "documents" and "resources"
are directories from `makeDirectory()`.

1. **Documents wins over the bundle.** Both directories hold the bundled scene: `source`
   is `.documents` and `plants.count == 1`.
2. **The bundle is used when Documents has none.** Only resources holds the bundled scene:
   `source` is `.bundle` and `plants.count == 1`.
3. **Embedded JSON when neither has one.** Both directories are empty: `source` is
   `.embeddedJSON` and `plants.count == 1`.
4. **Nil directories use the embedded JSON.**
   `loadScene(fromDocuments: nil, resources: nil, source: &source)`: `source` is
   `.embeddedJSON` and `plants.count == 1`.
5. **A plantless Documents scene falls through to the bundle.** Documents holds the
   plantless scene and resources holds the bundled one: `source` is `.bundle` and
   `plants.count == 1`.
6. **Plantless scenes everywhere use the embedded JSON.** Both directories hold the
   plantless scene: `source` is `.embeddedJSON` and `plants.count == 1`.
7. **The bundled scene matches the embedded one.** Load once with only resources holding
   the bundled scene (source `.bundle`), and once with `nil, nil` (source
   `.embeddedJSON`). The two scenes have equal `totalTime`, equal `plants.count`, and a
   first plant with equal `grammar.axiom.predecessor` and equal
   `grammar.productions.count`.
8. **Existing behaviour intact.** `SceneFileTests.swift` and every `LLabGeneratorTests`
   file are unmodified, and the whole `swift test` suite passes.

## test_strategy

- framework: swift_test
- required: true
- repo: llab
- base_ref: main
- protected_paths:
  - Package.swift
  - LLab.xcodeproj/project.pbxproj
  - LLab Shared/scene.usda
  - LLab Shared/Store/USDSceneParser.m
  - LLab Shared/Store/SceneParser.m
  - LLab Shared/Model/SceneModels.h
  - LLab Shared/Model/SceneModels.m
  - Tests/LLabStoreTests/SceneFileTests.swift
  - Tests/LLabStoreShim/README.md
  - Tests/LLabGeneratorTests/LSystemTests.swift
  - Tests/LLabTestSupport/include/LLabTestSupport.h
  - Tests/LLabTestSupport/LLabTestSupport.cpp

## Constraints

- `loadScene` keeps its signature and its callers do not change.
- Fix the plantless case in the loader (`usableSceneInDirectory:`), not in
  `USDSceneParser`, which is protected. A parser that rejects garbage is separate work.
- No new files outside the three in the change surface. A new Store `.m` would also need
  a new symlink in `Tests/LLabStoreShim/`, which is out of scope.
- The new test file never uses `@testable import`, and never redeclares `repoRoot` or
  `bundledSceneURL`.

## Risks

- **Redeclaring the shared constants.** `let bundledSceneURL = …` in the new file is an
  "invalid redeclaration" compile error, because `SceneFileTests.swift` already declares it
  in the same module.
- **Trusting a non-nil USD result.** `loadSceneFromUSD:` returns a zero-plant scene for
  garbage, so checking only `scene != nil` passes criteria 1–4 and fails 5 and 6. The
  plant count is the test.
- **Tests that read the real Documents directory.** `loadScene` reads the developer's
  actual `~/Documents`. Tests must call `loadScene(fromDocuments:resources:source:)` with
  their own directories, never `loadScene()`.
- **Shared directories across parallel tests.** Swift Testing runs tests concurrently, so
  a fixed temp path makes tests overwrite each other's `scene.usda`. Use a `UUID` per
  directory.
