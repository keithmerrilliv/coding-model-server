# LLab: a grown scene is written as USD geometry that ovxr can render

Jira: DEV-951, epic DEV-449. Repo `llab`, `main` = `fab96bb` (run 68's DEV-832 delivery plus
its re-indent). Every fact below was read from that commit.

A reference implementation was run on the Mac runner before submission (2026-11-02). On
`main` with only the new test file added, the build fails (`cannot find
'usdGeometryForSystem' in scope`). With the full change, `swift test` passes: the eight
new tests, the eight existing generator tests and the fifteen store tests. The writer's
output for the embedded plant opens in Pixar's USD library (`usd-core`) with the prim
types below, and its leaf mesh passes `UsdGeom.Mesh.ValidateTopology`. The acceptance
criteria are structural (prim presence, counts that must match `flatModules`, and a
hand-computed single-branch case), not values copied from that run.

## Context

**Why.** NVIDIA's ovxr 0.1 (early access, 2026-10) presents Omniverse RTX scenes on OpenXR
headsets. It takes a USD stage through `ovstage.population.open_usd`, so anything LLab can
write as USD geometry can be path-traced in a headset. ovxr runs only on Linux or Windows
x86_64 with an RTX GPU; LLab is a macOS Metal app. The seam between them is a file: LLab
grows the plant and writes geometry, ovxr on the RTX box reads it. This spec is the writer.
The export entry point in the app and the Linux-side viewer are follow-ups, below.

**What LLab has today.** `LLab Shared/scene.usda` is a *parameter* file: `Scope` prims
holding grammar, timing and geometry constants under `/SCENE/PLANT_n`. It carries no
renderable prim, so ovrtx would open it and draw nothing. `USDSceneExporter` writes that
same parameter form. Geometry exists only at runtime: `LSystem::step` grows the derivation
tree and `flattenTree` fills `LSystem::flatModules`, a `std::vector<FlatModule>`
(`LLab Shared/Generator/FlatModule.h`, served below), which `Renderer.mm` uploads to a
compute kernel. That kernel (`growAndEmitVertices` in `Shaders.metal`) is the authoritative
description of what a module looks like on screen, and the writer reproduces it on the CPU:

- `t = clamp((currentAge - initialAge) / moduleLifeSpan, 0, 1)`, and `t = 1` when
  `moduleLifeSpan <= 0`.
- A `GeomBranch` module is a line from `startPos` to `mix(startPos, endPos, t)`, width
  `diameter`, colour `colorIndex`.
- A `GeomLeaf` module with `numVertices >= 3` is a polygon whose vertex `v` is
  `mix(attachPos, endVerts[v], t)`, colour `leafColorIndex`. A leaf with fewer than three
  vertices is not drawn.
- `GeomNone` modules are skipped.
- Colour index → RGB, from `colorForIndex`: `1` → `(0.45, 0.19, 0.19)` brown, `2` →
  `(0.0, 1.0, 0.12)` green, `3` → `(1.0, 0.0, 0.0)` red, anything else → `(1.0, 1.0, 0.0)`
  yellow.

`mix(a, b, t)` is `a + (b - a) * t`, per component.

Module positions are plant-local: `createRootModule` starts the turtle at the origin, and
neither the generator nor the shader applies `xPos`/`zPos`. A plant's placement is therefore
a translate on its enclosing prim, which is also how the parameter file records it
(`xformOp:translate = (10, 0, 14)` on `PLANT_0`).

**The test harness.** `swift test` builds `LLab Shared/Generator` as the C++ module
`LLabGenerator` (`publicHeadersPath: "."`, so a new header there is public with no manifest
change) and `Tests/LLabTestSupport` as the bridge Swift's C++ interop needs. Tests in
`Tests/LLabGeneratorTests` create a system with `try #require(makeLSystem(...))`, hold it
in a `var`, and free it with `defer { destroyLSystem(system) }`. A `std::string` returned
to Swift converts with `String(...)`. The app builds from `LLab.xcodeproj`, which does not
read `Package.swift` and will not list the new `.cpp`; that is fine for this spec, since
nothing in the app calls it yet.

**Follow-ups, not in this spec.**

1. An export entry point: the `.cpp` added to the Xcode target and a way to write the file
   at the current simulation time (a menu item in the macOS app, or a SwiftPM executable
   that loads `scene.usda` through `LLabStore`, steps every plant to a given time and
   writes the result). Needs `project.pbxproj` or `Package.swift`, neither of which changes here.
2. A viewer on the RTX box: ovxr's `examples/python/minimal-xr` with `--usd <file>`,
   behind an OpenXR runtime. That part cannot run on the Mac runner and is proven by hand.

## Required change

### `LLab Shared/Generator/USDGeometryWriter.h`

New file, exactly:

```cpp
#ifndef USD_GEOMETRY_WRITER_H
#define USD_GEOMETRY_WRITER_H

#include "FlatModule.h"
#include <string>
#include <vector>

// One plant to write: its prim name, placement and the modules flattenTree produced.
struct USDPlantGeometry {
    std::string name;            // USD identifier, e.g. "PLANT_0"; written as given
    float xPos = 0.0f;
    float zPos = 0.0f;
    std::vector<FlatModule> modules;
};

// The complete USDA text for the given plants: a stage ovrtx/ovxr can open directly.
// metersPerUnit is written into the stage metadata (1 = LLab units are metres).
std::string writeUSDGeometry(const std::vector<USDPlantGeometry>& plants,
                             double metersPerUnit = 1.0);

#endif // USD_GEOMETRY_WRITER_H
```

No `using namespace std;` in this header (`LSystem.h` has one; do not add another).

### `LLab Shared/Generator/USDGeometryWriter.cpp`

New file. Implements `writeUSDGeometry`, with these rules.

**Stage.** The text starts with `#usda 1.0` and a metadata block holding `defaultPrim =
"LLab"`, `upAxis = "Y"` and `metersPerUnit = <metersPerUnit>`, the last printed with
`%g` (so `1` for the default and `0.01` for a centimetre stage). Then one `def Xform
"LLab"` encloses every plant, in order.

**Plant.** Each plant is a `def Xform "<name>"` whose first two attributes are a
`double3 xformOp:translate` of `(xPos, 0, zPos)` and `uniform token[] xformOpOrder =
["xformOp:translate"]`. A plant with no drawable module has those and nothing else.

**Branches.** If the plant has at least one `GeomBranch` module, it holds a `def
BasisCurves "Branches"` with, in this order: `uniform token type = "linear"`, `uniform
token wrap = "nonperiodic"`, `int[] curveVertexCounts` (one `2` per branch), `point3f[]
points` (per branch, its start then its grown end), `float[] widths` (the diameter, twice
per branch) and `color3f[] primvars:displayColor` with `interpolation = "uniform"` (one
colour per branch). No `GeomBranch` module, no `Branches` prim: an empty `points = []` is
not acceptable.

**Leaves.** If the plant has at least one `GeomLeaf` module with `numVertices >= 3`, it
holds a `def Mesh "Leaves"` with, in this order: `uniform token subdivisionScheme =
"none"`, `uniform bool doubleSided = 1`, `int[] faceVertexCounts` (`numVertices` per
leaf), `int[] faceVertexIndices` (consecutive from 0, leaf after leaf), `point3f[] points`
(the grown vertices, leaf after leaf) and `color3f[] primvars:displayColor` with
`interpolation = "uniform"` (one colour per leaf). A leaf with `numVertices < 3` is
skipped everywhere, as the kernel skips it.

**Numbers.** Every float other than `metersPerUnit` is printed with `%.6f` through
`std::snprintf` (never `std::to_string`, never a stream, so the format does not depend on
the locale or on stream state). The growth maths uses the same `t` and `mix` as the
kernel, above. Indent four spaces per level, as `scene.usda` does.

The reference writer produced exactly this for the embedded plant stepped 30 times and
placed at `(10, 14)`. It is the shape to match, not text to embed:

```
#usda 1.0
(
    defaultPrim = "LLab"
    upAxis = "Y"
    metersPerUnit = 1
)

def Xform "LLab"
{
    def Xform "PLANT_0"
    {
        double3 xformOp:translate = (10.000000, 0.000000, 14.000000)
        uniform token[] xformOpOrder = ["xformOp:translate"]

        def BasisCurves "Branches"
        {
            uniform token type = "linear"
            uniform token wrap = "nonperiodic"
            int[] curveVertexCounts = [2]
            point3f[] points = [(0.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000)]
            float[] widths = [1.000000, 1.000000]
            color3f[] primvars:displayColor = [(1.000000, 1.000000, 0.000000)] (
                interpolation = "uniform"
            )
        }

        def Mesh "Leaves"
        {
            uniform token subdivisionScheme = "none"
            uniform bool doubleSided = 1
            int[] faceVertexCounts = [6, 6]
            int[] faceVertexIndices = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
            point3f[] points = [(0.000000, 1.000000, 0.000000), (0.536757, 1.582563, 0.610338), (0.669779, 2.225351, 1.364744), (0.374140, 2.807914, 2.121856), (-0.162617, 2.225351, 1.511518), (-0.295639, 1.582563, 0.757111), (0.000000, 1.000000, 0.000000), (-0.807830, 1.582563, 0.089610), (-1.502102, 2.225351, -0.234135), (-1.952720, 2.807914, -0.910569), (-1.144890, 2.225351, -1.000179), (-0.450618, 1.582563, -0.676434)]
            color3f[] primvars:displayColor = [(0.450000, 0.190000, 0.190000), (0.450000, 0.190000, 0.190000)] (
                interpolation = "uniform"
            )
        }
    }
}
```

### `Tests/LLabTestSupport/include/LLabTestSupport.h`

Modified, one edit. Replace this line, which occurs once:

```cpp
void addProduction(ProductionMap& productions, const std::string& predecessor, const std::string& successor);
```

with:

```cpp
void addProduction(ProductionMap& productions, const std::string& predecessor, const std::string& successor);

// USD geometry for one system as it stands now (its flatModules), under the prim `name`
// placed at (xPos, 0, zPos). metersPerUnit is passed through to the stage metadata.
std::string usdGeometryForSystem(TestLSystem* system, const std::string& name,
                                 float xPos, float zPos, double metersPerUnit);

// USD geometry for a single hand-built branch module, so a test can check the growth
// maths and the colour table against known inputs without growing a plant.
std::string usdGeometryForBranch(float sx, float sy, float sz, float ex, float ey, float ez,
                                 float diameter, float colorIndex,
                                 float currentAge, float initialAge, float moduleLifeSpan);
```

### `Tests/LLabTestSupport/LLabTestSupport.cpp`

Modified, two edits. First, replace the line `#include "LLabTestSupport.h"`, which occurs
once, with:

```cpp
#include "LLabTestSupport.h"
#include "USDGeometryWriter.h"
```

Second, replace the whole `addProduction` definition, these four lines, which occur once:

```cpp
void addProduction(ProductionMap& productions, const std::string& predecessor, const std::string& successor)
{
    productions[predecessor] = successor;
}
```

with:

```cpp
void addProduction(ProductionMap& productions, const std::string& predecessor, const std::string& successor)
{
    productions[predecessor] = successor;
}

std::string usdGeometryForSystem(TestLSystem* system, const std::string& name,
                                 float xPos, float zPos, double metersPerUnit)
{
    USDPlantGeometry plant;
    plant.name = name;
    plant.xPos = xPos;
    plant.zPos = zPos;
    plant.modules = system->flatModules;
    return writeUSDGeometry({plant}, metersPerUnit);
}

std::string usdGeometryForBranch(float sx, float sy, float sz, float ex, float ey, float ez,
                                 float diameter, float colorIndex,
                                 float currentAge, float initialAge, float moduleLifeSpan)
{
    FlatModule fm = {};
    fm.geomType = GeomBranch;
    fm.startPos[0] = sx; fm.startPos[1] = sy; fm.startPos[2] = sz;
    fm.endPos[0] = ex;   fm.endPos[1] = ey;   fm.endPos[2] = ez;
    fm.diameter = diameter;
    fm.colorIndex = colorIndex;
    fm.currentAge = currentAge;
    fm.initialAge = initialAge;
    fm.moduleLifeSpan = moduleLifeSpan;
    USDPlantGeometry plant;
    plant.name = "BRANCH";
    plant.modules.push_back(fm);
    return writeUSDGeometry({plant}, 1.0);
}
```

### `Tests/LLabGeneratorTests/USDGeometryWriterTests.swift`

New file. A Swift Testing file with exactly these imports, the same four as
`LSystemTests.swift`:

```swift
import Testing
import LLabGenerator
import LLabTestSupport
import CxxStdlib
```

There is no `import Foundation`, so Foundation's string methods do not compile here:
`components(separatedBy:)`, `range(of:)` and `trimmingCharacters(in:)` all fail to build.
Use the standard library: `text.ranges(of: needle).count` to count occurrences,
`text.contains(...)` and `text.hasPrefix(...)` to match, and `line.firstRange(of: " = [")`
with `line.lastIndex(of: "]")` to slice an array attribute's entries. Slice from `" = ["`,
not from the first `[`: the attribute's own type (`int[]`, `point3f[]`) contains brackets.

`LSystemTests.swift` declares `embeddedPlant()` as `private`, so this file declares its own
private copy with the same four productions; it must not try to call the one in
`LSystemTests.swift`:

```swift
private func embeddedPlant() -> ProductionMap {
    var productions = ProductionMap()
    addProduction(&productions, std.string("(A,1)"), std.string("(I,0)[(B,1)]/////[(B,1)]/////[(B,1)][(X,1)]/////[(X,1)]"))
    addProduction(&productions, std.string("(I,1)"), std.string("F"))
    addProduction(&productions, std.string("(B,2)"), std.string("!f&[(A,0)]"))
    addProduction(&productions, std.string("(X,2)"), std.string("f////&&{'-f+f+f-|-f+f+f}"))
    return productions
}
```

Count modules by iterating `system.flatModules` and comparing `module.geomType` with
`GeomBranch` and `GeomLeaf`. Convert every returned `std.string` with `String(...)` before
asserting on it. One `@Test` per acceptance criterion below.

## Change surface

The two modified files were verified to exist at `main`, and the three new paths were
verified NOT to exist. The plan's implement phase lists exactly these five as outputs.

| Path | Change |
| --- | --- |
| `LLab Shared/Generator/USDGeometryWriter.h` | new |
| `LLab Shared/Generator/USDGeometryWriter.cpp` | new |
| `Tests/LLabTestSupport/include/LLabTestSupport.h` | modified (one edit) |
| `Tests/LLabTestSupport/LLabTestSupport.cpp` | modified (two edits) |
| `Tests/LLabGeneratorTests/USDGeometryWriterTests.swift` | new |

Each anchor quoted above occurs exactly once in its file at `main`. The protected files
below are served read-only, for reference. Do not edit them, and do not edit any file
outside the five above.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile (`usdGeometryForSystem` and `usdGeometryForBranch` do not exist), and the
report must say so.

1. **The stage is openable metadata-first.** For the embedded plant stepped 30 times
   (`system.step(Float(tick) * 0.1)` for `tick in 1...30`, as `embeddedPlantGrowsGeometry`
   does), `usdGeometryForSystem(system, "PLANT_0", 10, 14, 1.0)` starts with `#usda 1.0`
   and contains `defaultPrim = "LLab"`, `upAxis = "Y"` and `metersPerUnit = 1`.
2. **Placement is a translate on the plant prim.** The same output contains
   `def Xform "PLANT_0"` and `double3 xformOp:translate = (10.000000, 0.000000, 14.000000)`.
3. **Every branch module is one linear curve.** The same output contains
   `def BasisCurves "Branches"` and `uniform token type = "linear"`, and the number of
   entries in `curveVertexCounts` equals the number of modules in `system.flatModules` with
   `geomType == GeomBranch`, each entry being `2`. (After 30 steps the plant has one.)
4. **Every drawable leaf is one face.** In the same output, `def Mesh "Leaves"` is present
   exactly when `system.flatModules` holds a module with `geomType == GeomLeaf` and
   `numVertices >= 3`, and then the number of entries in `faceVertexCounts` equals the
   number of such modules. (After 30 steps the plant has two.)
5. **An ungrown plant is an empty prim.** For a freshly made system with no `step`,
   `usdGeometryForSystem(system, "PLANT_0", 0, 0, 1.0)` contains `def Xform "PLANT_0"` and
   contains neither `def BasisCurves` nor `def Mesh`.
6. **Growth and colour match the kernel.** `usdGeometryForBranch(0, 0, 0, 0, 2, 0, 0.1, 1,
   5, 0, 10)` (a branch half way through a 10-unit lifespan, colour index 1) contains
   `points = [(0.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000)]`,
   `widths = [0.100000, 0.100000]` and `(0.450000, 0.190000, 0.190000)`.
7. **A spent lifespan clamps.** `usdGeometryForBranch(0, 0, 0, 0, 2, 0, 0.1, 2, 30, 0, 10)`
   contains `(0.000000, 2.000000, 0.000000)` and `(0.000000, 1.000000, 0.120000)`; and with
   `moduleLifeSpan` 0 (`..., 2, 0, 0, 0`) the end point is also `(0.000000, 2.000000,
   0.000000)`.
8. **The metadata knob is honoured.** `usdGeometryForSystem(system, "PLANT_0", 0, 0, 0.01)`
   contains `metersPerUnit = 0.01`.
9. **Existing behaviour intact.** Every other test file is unmodified, and the whole
   `swift test` suite passes, `LLabGeneratorTests` and `LLabStoreTests` both.

## test_strategy

- framework: swift_test
- required: true
- repo: llab
- base_ref: main
- protected_paths:
  - Package.swift
  - LLab Shared/Generator/FlatModule.h
  - LLab Shared/Generator/LSystem.h
  - Tests/LLabGeneratorTests/LSystemTests.swift

## Constraints

- The writer depends on `FlatModule.h` and the C++ standard library only: no USD SDK, no
  Foundation, no Objective-C. It must compile as C++17 in both the SwiftPM target and,
  later, the Xcode target.
- `LSystem`, `FlatModule`, `Shaders.metal` and `USDSceneExporter` do not change. The
  parameter file format is untouched; this is a second, separate output.
- Prim names are written as given. The writer does not validate or escape them.
- No new files outside the five in the change surface. In particular no `Package.swift`
  change: `LLabGenerator`'s `publicHeadersPath: "."` already exports the new header.
- The new test file never redeclares a symbol `LSystemTests.swift` makes public, and keeps
  its own `embeddedPlant()` private.

## Risks

- **`std::to_string` or `<<` for floats.** Both are locale-sensitive and `to_string` prints
  six decimals only by accident of `%f`. Criteria 6 and 7 pin the exact text; use
  `snprintf("%.6f")`.
- **Growth read from `endPos` directly.** The kernel draws to `mix(start, end, t)`, not to
  `endPos`; a branch written at its final length is wrong for every module still growing.
  Criterion 6 catches it.
- **Emitting empty arrays.** `points = []` on a `BasisCurves` prim is invalid USD for
  ovrtx's purposes and criterion 5 forbids the prim outright.
- **Leaves with fewer than three vertices.** `flattenTree` records them; the kernel does
  not draw them. Writing them would produce a degenerate face and break criterion 4's
  count.
- **Foundation string methods in the test.** They do not compile with the four imports;
  the reference test's first build failed on exactly that. See the test file's section.
- **Slicing from the first `[`.** It lands inside `int[]`, and the entry count can still
  come out right by accident while the entries are wrong. Slice from `" = ["`.
- **`using namespace std;` in the new header.** It would compile, but it leaks into every
  file that includes it, and the repo already pays for one such header.
- **Calling `embeddedPlant()` across test files.** It is `private` in `LSystemTests.swift`
  and invisible; the new file needs its own copy.
- **Passing a `std::vector<USDPlantGeometry>` from Swift.** Swift's C++ interop does not
  spell that type comfortably, which is why the bridge takes a `TestLSystem*` and scalars.
  Tests go through the bridge only.
