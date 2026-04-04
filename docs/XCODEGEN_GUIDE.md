# Xcode Project Generation Guide for AI Agents

Two tools are available: **XcodeGen** (YAML-based, simpler) and **Tuist** (Swift-based, more powerful). Use XcodeGen for quick scaffolding; use Tuist for complex multi-module projects that benefit from type-safe manifests and binary caching.

---

# Part 1: XcodeGen

## What is XcodeGen?

XcodeGen generates `.xcodeproj` files from a `project.yml` spec. It scans your source directories and automatically adds files to the correct build phases based on extension (`.swift` → Compile Sources, `.metal` → Compile Sources, images/xibs → Resources). The `.xcodeproj` is regenerated on demand and should be gitignored.

## CLI Usage

```bash
# Generate from project.yml in current directory
xcodegen generate

# Specify spec file and output directory
xcodegen generate --spec my_spec.yml --project ./output

# Use cache to skip if nothing changed
xcodegen generate --use-cache

# Dump resolved spec
xcodegen dump --type json
```

## Minimal project.yml

```yaml
name: MyApp
options:
  bundleIdPrefix: com.example
  deploymentTarget:
    macOS: "14.0"
targets:
  MyApp:
    type: application
    platform: macOS
    sources: [Sources]
    dependencies:
      - sdk: Metal.framework
```

## Full project.yml Structure

### Top-level keys

| Key | Required | Description |
|-----|----------|-------------|
| `name` | Yes | Name of the generated `.xcodeproj` |
| `options` | No | Global options (bundle ID prefix, deployment targets, etc.) |
| `configs` | No | Build configurations. Defaults: `Debug: debug`, `Release: release` |
| `settings` | No | Project-level build settings |
| `settingGroups` | No | Reusable named groups of build settings |
| `targets` | No | Map of target name to target definition |
| `schemes` | No | Explicit scheme definitions |
| `packages` | No | Swift Package dependencies (remote or local) |
| `include` | No | Paths to other spec files to merge in |

### Options

```yaml
options:
  bundleIdPrefix: com.mycompany
  deploymentTarget:
    iOS: "15.0"
    macOS: "14.0"
    visionOS: "2.0"
  createIntermediateGroups: true
  xcodeVersion: "1600"
  developmentLanguage: en
  defaultConfig: Debug
  postGenCommand: pod install    # Runs after generation
```

### Targets

```yaml
targets:
  MyApp:
    type: application          # REQUIRED
    platform: macOS            # REQUIRED (or use supportedDestinations)
    deploymentTarget: "14.0"
    sources:
      - Sources                # Simple directory
      - path: SharedSources    # Advanced: with options
        excludes: ["**/*.md"]
        compilerFlags: ["-Werror"]
    dependencies:
      - target: MyFramework
      - package: Yams
      - sdk: Metal.framework
      - sdk: MetalKit.framework
      - framework: Vendor/Foo.framework
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: com.example.myapp
        SWIFT_VERSION: "5.9"
      configs:
        Debug:
          SWIFT_ACTIVE_COMPILATION_CONDITIONS: DEBUG
        Release:
          SWIFT_OPTIMIZATION_LEVEL: -O
    info:
      path: MyApp/Info.plist
      properties:
        UILaunchStoryboardName: LaunchScreen
    entitlements:
      path: MyApp/App.entitlements
      properties:
        com.apple.security.app-sandbox: true
    scheme:
      testTargets: [MyAppTests]
      configVariants: [Debug, Release]
```

**Product types:** `application`, `framework`, `library.static`, `library.dynamic`, `bundle`, `bundle.unit-test`, `tool`, `app-extension`, `extensionkit-extension`

**Platforms:** `iOS`, `macOS`, `tvOS`, `watchOS`, `visionOS`

**Multi-platform:** Use `supportedDestinations: [iOS, macOS, visionOS]` for a single multi-destination target.

### Sources (detailed)

```yaml
sources:
  - Sources                              # All files in Sources/
  - path: OtherSources
    excludes: ["**/*.md", "tests/**"]    # Glob excludes
    includes: ["**/*.swift"]             # Glob includes
    compilerFlags: ["-Werror"]
    buildPhase: sources                  # sources | resources | headers | copyFiles | none
    type: group                          # group | file | folder | syncedFolder
    optional: true                       # Don't error if missing
```

### Dependencies

```yaml
dependencies:
  # Another target in the project
  - target: MyFramework

  # System SDK frameworks and libraries
  - sdk: Metal.framework
  - sdk: MetalKit.framework
  - sdk: MetalPerformanceShaders.framework
  - sdk: libc++.tbd

  # Swift Package
  - package: Yams

  # Pre-built framework/XCFramework
  - framework: Vendor/Foo.framework
    embed: true

  # Linking options (any dependency type)
  - target: MyFramework
    embed: true
    link: true
    weak: false
```

### Build Settings

Three forms (do NOT mix simple with structured):

```yaml
# Simple (all configs)
settings:
  PRODUCT_NAME: MyProduct

# Structured (base + per-config)
settings:
  base:
    PRODUCT_NAME: MyProduct
  configs:
    Debug:
      SWIFT_ACTIVE_COMPILATION_CONDITIONS: DEBUG
    Release:
      SWIFT_OPTIMIZATION_LEVEL: -O

# With groups
settings:
  groups: [common_settings]
  base:
    PRODUCT_NAME: MyProduct
```

### Setting Groups (reusable presets)

```yaml
settingGroups:
  common:
    DEVELOPMENT_TEAM: ABCDEF123
    SWIFT_VERSION: "5.9"
  metal_project:
    MTL_ENABLE_DEBUG_INFO: INCLUDE_SOURCE
    MTL_FAST_MATH: YES
```

### Schemes

**Simple (on target):**
```yaml
targets:
  MyApp:
    scheme:
      testTargets: [MyAppTests]
      gatherCoverageData: true
```

**Full control:**
```yaml
schemes:
  MyAppScheme:
    build:
      targets:
        MyApp: all
        MyFramework: [run, test]
    run:
      config: Debug
      executable: MyApp
      enableGPUFrameCaptureMode: metal
      enableGPUValidationMode: true
      environmentVariables:
        ENV: development
    test:
      config: Debug
      targets:
        - MyAppTests
        - name: MyUITests
          parallelizable: true
    archive:
      config: Release
```

### Swift Packages

```yaml
packages:
  Yams:
    url: https://github.com/jpsim/Yams
    from: 2.0.0
  Ink:
    github: JohnSundell/Ink      # GitHub shorthand
    from: 0.5.0
  MyLocalPkg:
    path: ../MyLocalPkg          # Local package
```

### Build Scripts

```yaml
targets:
  MyApp:
    preBuildScripts:
      - script: echo "Pre-build"
        name: Pre-build
    postBuildScripts:
      - path: scripts/post_build.sh
        name: Post Build
```

### Configs

```yaml
configs:
  Debug: debug       # Gets debug build presets
  Release: release   # Gets release build presets
  Staging: release   # Custom config with release presets
```

## Patterns for Swift + Metal Projects

### macOS Metal Application

```yaml
name: MetalApp
options:
  bundleIdPrefix: com.example
  deploymentTarget:
    macOS: "14.0"
  createIntermediateGroups: true

targets:
  MetalApp:
    type: application
    platform: macOS
    sources:
      - Sources
    dependencies:
      - sdk: Metal.framework
      - sdk: MetalKit.framework
    settings:
      base:
        SWIFT_VERSION: "5.9"
        MTL_ENABLE_DEBUG_INFO: INCLUDE_SOURCE
    scheme:
      configVariants: [Debug, Release]
      run:
        enableGPUFrameCaptureMode: metal
        enableGPUValidationMode: true
```

### visionOS Immersive App

```yaml
name: VisionApp
options:
  bundleIdPrefix: com.example
  deploymentTarget:
    visionOS: "2.0"

targets:
  VisionApp:
    type: application
    platform: visionOS
    sources:
      - Sources
    dependencies:
      - sdk: Metal.framework
      - sdk: CompositorServices.framework
      - sdk: Spatial.framework
    settings:
      base:
        SWIFT_VERSION: "5.9"
        SUPPORTED_PLATFORMS: xros xrsimulator
    entitlements:
      path: VisionApp.entitlements
      properties:
        com.apple.developer.spatial-audio.capture: true
```

### Multi-platform (macOS + visionOS)

```yaml
name: CrossPlatform
options:
  bundleIdPrefix: com.example

targets:
  CrossPlatform:
    type: application
    supportedDestinations: [macOS, visionOS]
    sources:
      - Sources
    dependencies:
      - sdk: Metal.framework
      - sdk: MetalKit.framework
    settings:
      base:
        SWIFT_VERSION: "5.9"
```

## Key Facts for Agents

1. **Metal shaders (.metal files) are auto-detected** as compilable sources. No special config needed.
2. **The .xcodeproj should be gitignored.** Regenerate with `xcodegen generate`.
3. **`project.yml` is the single source of truth** for the Xcode project structure.
4. **Source directories are scanned recursively.** Files are assigned to build phases by extension.
5. **Sensible defaults are applied.** You only need to specify what you want to customize.
6. **Generate and build in one command:** `xcodegen generate && xcodebuild -project Foo.xcodeproj -scheme Foo build`
7. **Do NOT manually edit .xcodeproj files.** Always edit project.yml and regenerate.
8. **Use `info:` to auto-generate Info.plist** instead of maintaining a separate file.
9. **GPU debugging:** Set `enableGPUFrameCaptureMode: metal` and `enableGPUValidationMode: true` in the scheme's run action.
10. **xcconfig files** can be referenced via `configFiles:` for complex build settings.

---

# Part 2: Tuist

## What is Tuist?

Tuist generates `.xcodeproj`/`.xcworkspace` from **Swift manifest files** (`Project.swift`). It provides type-safe project definitions with Xcode autocompletion, plus built-in binary caching, selective testing, and dependency graph validation. More powerful than XcodeGen but higher learning curve.

## When to Use Tuist vs XcodeGen

| Scenario | Use |
|----------|-----|
| Quick single-target app | XcodeGen (simpler YAML) |
| Multi-module project | Tuist (better dependency management) |
| CI with caching needs | Tuist (built-in binary cache) |
| Agent scaffolding new project | XcodeGen (faster, less setup) |
| Long-lived project with many contributors | Tuist (type safety, graph validation) |

## CLI Usage

```bash
# Initialize a new project
tuist init --platform ios --name MyApp

# Generate Xcode project from manifests
tuist generate
tuist generate --no-open    # Don't open Xcode

# Edit manifests with Xcode autocompletion
tuist edit

# Build (generates if needed)
tuist build

# Test (selective: only changed modules)
tuist test

# Resolve SPM dependencies
tuist install

# Warm binary cache
tuist cache

# Clean generated artifacts
tuist clean
```

## Directory Structure

```
MyProject/
  Tuist.swift                        # Global config (marks repo root)
  Tuist/
    ProjectDescriptionHelpers/       # Shared Swift helper code
    Package.swift                    # SPM dependencies
  Sources/                           # App source files
  Tests/                             # Test files
  Project.swift                      # Project manifest
```

## Minimal Project.swift

```swift
import ProjectDescription

let project = Project(
    name: "MyApp",
    targets: [
        .target(
            name: "MyApp",
            destinations: .macOS,
            product: .app,
            bundleId: "com.example.myapp",
            sources: ["Sources/**"],
            dependencies: [
                .sdk(name: "Metal", type: .framework),
            ]
        )
    ]
)
```

## Target Definition

```swift
.target(
    name: "MyApp",
    destinations: .macOS,              // .iOS, .macOS, [.mac, .iPhone]
    product: .app,                     // .framework, .staticFramework, .unitTests, .tool
    bundleId: "com.example.myapp",
    infoPlist: .extendingDefault(with: [
        "UILaunchScreen": [:],
    ]),
    sources: ["Sources/**"],           // .metal files auto-detected
    resources: ["Resources/**"],
    dependencies: [
        .target(name: "MyFramework"),
        .external(name: "Alamofire"),  // SPM package (after tuist install)
        .sdk(name: "Metal", type: .framework),
        .sdk(name: "MetalKit", type: .framework),
    ],
    settings: .settings(
        base: [
            "SWIFT_VERSION": "5.9",
            "MTL_ENABLE_DEBUG_INFO": "INCLUDE_SOURCE",
        ],
        configurations: [
            .debug(name: "Debug"),
            .release(name: "Release"),
        ]
    )
)
```

## Dependencies

```swift
// Same project
.target(name: "MyFramework")

// Another project in workspace
.project(target: "Lib", path: "../Lib")

// External SPM (after tuist install)
.external(name: "Alamofire")

// System SDK
.sdk(name: "Metal", type: .framework)
.sdk(name: "MetalKit", type: .framework)
.sdk(name: "libc++", type: .library)

// Pre-built
.xcframework(path: "Vendor/Foo.xcframework")
```

## External SPM Dependencies

Create `Tuist/Package.swift`:

```swift
// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Dependencies",
    dependencies: [
        .package(url: "https://github.com/Alamofire/Alamofire", from: "5.0.0"),
    ]
)
```

Then run `tuist install` to resolve.

## Swift + Metal Project Pattern

```swift
import ProjectDescription

let project = Project(
    name: "MetalRenderer",
    targets: [
        .target(
            name: "MetalRenderer",
            destinations: [.mac, .appleVision],
            product: .app,
            bundleId: "com.example.metalrenderer",
            infoPlist: .extendingDefault(with: [:]),
            sources: ["Sources/**"],       // .swift and .metal auto-detected
            resources: ["Resources/**"],
            dependencies: [
                .sdk(name: "Metal", type: .framework),
                .sdk(name: "MetalKit", type: .framework),
                .sdk(name: "MetalPerformanceShaders", type: .framework),
                .sdk(name: "CompositorServices", type: .framework,
                     condition: .when([.appleVision])),
            ],
            settings: .settings(
                base: [
                    "MTL_ENABLE_DEBUG_INFO": "INCLUDE_SOURCE",
                    "MTL_FAST_MATH": "YES",
                ]
            )
        ),
        .target(
            name: "MetalRendererTests",
            destinations: [.mac],
            product: .unitTests,
            bundleId: "com.example.metalrenderer.tests",
            sources: ["Tests/**"],
            dependencies: [.target(name: "MetalRenderer")]
        )
    ]
)
```

## Reusable Helpers (ProjectDescriptionHelpers/)

```swift
// Tuist/ProjectDescriptionHelpers/TargetFactory.swift
import ProjectDescription

extension Target {
    static func metalApp(name: String, bundleId: String) -> Target {
        .target(
            name: name,
            destinations: .macOS,
            product: .app,
            bundleId: bundleId,
            sources: ["Sources/**"],
            dependencies: [
                .sdk(name: "Metal", type: .framework),
                .sdk(name: "MetalKit", type: .framework),
            ],
            settings: .settings(base: [
                "MTL_ENABLE_DEBUG_INFO": "INCLUDE_SOURCE",
            ])
        )
    }
}
```

## Key Facts for Agents

1. **Metal shaders (.metal) are auto-detected** in `sources` globs. No special config needed.
2. **The .xcodeproj should be gitignored.** Regenerate with `tuist generate`.
3. **`tuist edit`** opens manifests in Xcode with full autocompletion — useful for verifying syntax.
4. **`tuist build`** generates + builds in one command. Equivalent to `tuist generate && xcodebuild`.
5. **Type safety:** Swift compiler catches manifest errors before generation. No runtime YAML surprises.
6. **Binary caching:** `tuist cache` pre-builds modules. CI can reuse cached binaries for unchanged modules.
7. **Selective testing:** `tuist test` only runs tests for modules affected by code changes.
8. **ProjectDescriptionHelpers** allow real Swift factory methods — much more powerful than XcodeGen YAML templates.
9. **Platform conditions** on dependencies: `.sdk(name: "CompositorServices", type: .framework, condition: .when([.appleVision]))`.
10. **Commercial features**: Binary caching, selective testing, and insights require Tuist Server (cloud). The CLI generation is fully open source.
