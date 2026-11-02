# Electric Sheep: the app bundle does not ship the repository's Markdown files

Jira: DEV-864 item 4 (audit 2026-09-27). Repo `electric-sheep`, `main` = `fc3903d`. Every
fact below was read from that commit.

A reference implementation of exactly the edit below, with exactly the test file below,
ran on the Mac Studio (macOS host, `platform=macOS`) before this spec was written:

| Tree | `AppBundleContentsTests` | Whole `ElectricSheepTests` |
| --- | --- | --- |
| unmodified `main` + new test file | 1 passed, 6 failed (`noMarkdownInBundle`, and `namedProseFileAbsent` for all five names) | not run |
| `main` + the edit below + new test file | 7 passed | TEST SUCCEEDED; only the opt-in `LiveGenerationTests` skips |

No run was made on the Vision Pro. Do not re-derive any of this.

## Context

`ElectricSheep.xcodeproj/project.pbxproj` puts five prose files from the repository root
in the app target's Resources build phase, so each is copied into `ElectricSheep.app`:
`README.md`, `ARCHITECTURE.md`, `ARCHITECTURE.ja.md`, `AUDIT_REPORT.md`,
`IMPLEMENTER_PLAN.md`. Nothing reads them at runtime. Each is declared twice in the
project:

- a `PBXBuildFile` object ("... in Resources"), in the `PBXBuildFile` section, lines 14-18;
- a line in the app target's `PBXResourcesBuildPhase` `files` list (object
  `F3107FC92F7F25FC0066397B`), lines 250-254.

Removing those ten lines and nothing else stops the copy. The `PBXFileReference` objects
and the group entries stay, so the five files remain visible in Xcode's navigator and in
git; this change is about what ships, not about the files.

The other half of DEV-864 (rewriting the docs, archiving two of them) is NOT part of this
spec.

## The two files — how to emit them

- `ElectricSheep.xcodeproj/project.pbxproj` exists and is about 700 lines. **Emit its
  change as exactly two anchored SEARCH/REPLACE blocks, never as a whole file.** Each
  SEARCH block is one of the two quoted runs below, copied exactly (they are indented
  with TAB characters). Each REPLACE block is empty: the run is deleted.
- `ElectricSheepTests/AppBundleContentsTests.swift` does NOT exist. **Emit it as ONE
  complete whole-file block** (`<<<FILE: ...>>>` ... END_FILE), never as SEARCH/REPLACE.
  The test target is a file-system synchronized folder, so a new file there needs no
  project edit.

## Edit 1 — `ElectricSheep.xcodeproj/project.pbxproj`, two deletions

Delete this run of five consecutive lines (the `PBXBuildFile` section). It is unique in
the file.

```
		F31080302F7F29F90066397B /* AUDIT_REPORT.md in Resources */ = {isa = PBXBuildFile; fileRef = F310802E2F7F29F90066397B /* AUDIT_REPORT.md */; };
		F31080312F7F29F90066397B /* ARCHITECTURE.ja.md in Resources */ = {isa = PBXBuildFile; fileRef = F310802D2F7F29F90066397B /* ARCHITECTURE.ja.md */; };
		F31080322F7F29F90066397B /* ARCHITECTURE.md in Resources */ = {isa = PBXBuildFile; fileRef = F310802C2F7F29F90066397B /* ARCHITECTURE.md */; };
		F31080332F7F29F90066397B /* README.md in Resources */ = {isa = PBXBuildFile; fileRef = F310802B2F7F29F90066397B /* README.md */; };
		F31080342F7F29F90066397B /* IMPLEMENTER_PLAN.md in Resources */ = {isa = PBXBuildFile; fileRef = F310802F2F7F29F90066397B /* IMPLEMENTER_PLAN.md */; };
```

Delete this run of five consecutive lines (inside the app target's Resources phase
`files` list, between `files = (` and `);`). It is unique in the file.

```
				F31080302F7F29F90066397B /* AUDIT_REPORT.md in Resources */,
				F31080312F7F29F90066397B /* ARCHITECTURE.ja.md in Resources */,
				F31080322F7F29F90066397B /* ARCHITECTURE.md in Resources */,
				F31080332F7F29F90066397B /* README.md in Resources */,
				F31080342F7F29F90066397B /* IMPLEMENTER_PLAN.md in Resources */,
```

Nothing else in `project.pbxproj` changes: not the `PBXFileReference` lines for the five
files, not the group's `children` list, not the test target's or the UI test target's
Resources phases.

## Edit 2 — new test file `ElectricSheepTests/AppBundleContentsTests.swift`

Create it with exactly this content:

```swift
import Testing
import Foundation
@testable import ElectricSheep

/// The app bundle ships what the app reads. The project's Resources phase once copied the
/// repository's five prose files into `ElectricSheep.app`, where nothing opens them; this
/// pins that they stay out, so a file added to the phase by dragging it into Xcode shows up
/// as a red test rather than as dead weight in a shipped binary.
@Suite("App bundle contents")
struct AppBundleContentsTests {

    /// `HalluRenderer` is compiled into the app target, so its bundle is the app's own
    /// whether the tests run hosted on macOS or on a device.
    private static let appBundle = Bundle(for: HalluRenderer.self)

    @Test("no Markdown file is copied into the app bundle")
    func noMarkdownInBundle() {
        let found = Self.appBundle.urls(forResourcesWithExtension: "md", subdirectory: nil) ?? []
        #expect(found.map(\.lastPathComponent).sorted().isEmpty,
                "Markdown files shipped in the app bundle: \(found.map(\.lastPathComponent))")
    }

    @Test("the repository's prose files are not in the bundle under any name",
          arguments: ["README", "ARCHITECTURE", "ARCHITECTURE.ja", "AUDIT_REPORT", "IMPLEMENTER_PLAN"])
    func namedProseFileAbsent(name: String) {
        #expect(Self.appBundle.url(forResource: name, withExtension: "md") == nil)
    }

    @Test("the bundle under test is the app, not an empty stand-in")
    func bundleIsTheApp() {
        #expect(Self.appBundle.bundleIdentifier?.isEmpty == false)
        #expect(Self.appBundle.executableURL != nil)
    }
}
```

## Change surface

| Path | Change |
| --- | --- |
| `ElectricSheep.xcodeproj/project.pbxproj` | modified: the two deletions in Edit 1, as two anchored SEARCH/REPLACE blocks |
| `ElectricSheepTests/AppBundleContentsTests.swift` | new, whole-file block |

## Protected paths — must not be modified

Everything under `ElectricSheep/` (app sources and `Shaders.metal`), every other file in
`ElectricSheepTests/` and `ElectricSheepUITests/`, and the five Markdown files themselves.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. The notes say how each behaved on
unmodified `main` in the reference run, and the report must say the same.

1. **No Markdown in the bundle.** `noMarkdownInBundle` passes. On `main` it fails, listing
   the five files.
2. **None of the five, by name.** `namedProseFileAbsent` passes for all five arguments. On
   `main` all five fail.
3. **The test looks at the app.** `bundleIsTheApp` passes, on `main` too: it guards against
   the two tests above passing because they inspected an empty bundle.
4. **Nothing else moved.** The diff of `project.pbxproj` is exactly ten deleted lines and
   no added lines. The five `PBXFileReference` lines are still present.
5. **Existing suites unchanged.** The whole `ElectricSheepTests` suite is green on macOS;
   the opt-in `LiveGenerationTests` skips, as on `main`.

## test_strategy

```yaml
framework: xcodebuild_test
required: true
repo: electric-sheep
base_ref: main
scheme: ElectricSheep
destination: "platform=macOS"
filter: ElectricSheepTests
default_actor_isolation: MainActor
protected_paths:
  - ElectricSheep/Shaders.metal
  - ElectricSheep/HalluRenderer.swift
  - ElectricSheepTests/ElectricSheepTests.swift
  - README.md
  - ARCHITECTURE.md
  - ARCHITECTURE.ja.md
  - AUDIT_REPORT.md
  - IMPLEMENTER_PLAN.md
```

## Constraints

- No new dependencies. Swift Testing (`import Testing`) for the new file, not XCTest.
- Do not add `#if os(...)`: the check is the same on every platform.
- Do not touch the `PBXFileReference` objects, the group, or any other build phase.
- Do not edit, move or delete the Markdown files; this spec only stops them shipping.

## Risks

- **Rewriting the project file whole.** The file is long and tab-indented; a whole-file
  emission risks changing unrelated lines. Use the two anchored deletions.
- **Deleting the wrong run.** `README.md in Resources` style lines appear only in the two
  runs above, but the `PBXFileReference` lines for the same files look similar. Delete only
  lines ending in `in Resources */,` or containing `isa = PBXBuildFile`.
