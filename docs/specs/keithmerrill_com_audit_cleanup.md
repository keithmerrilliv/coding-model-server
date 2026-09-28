# keithmerrill.com: the hero frees its geometry, and the template leftovers go

Jira: DEV-300, epic DEV-447. Repo `keithmerrill-com`, `main` = `1ea3fe7` ("Add launch
metadata: robots, sitemap, OG image"). Every fact below was read from that commit. A
reference implementation of exactly the edits below, with the tests described, ran before
this spec was written, through this pipeline's own sandboxed vitest path on a workspace
holding only the nine files in the change surface: 8 of 8 passed. The new test file
against unmodified `main` fails to load
(`Failed to resolve import "./treeGeometry" from "lib/treeGeometry.test.ts"`). With the
reference's `useEffect` removed, exactly the two disposal tests fail. On the full tree,
`tsc --noEmit`, `eslint` and `next build` are clean. Do not re-derive the expected values.

This is the first spec for this repository, and the first to run vitest on this server
against an existing TypeScript repo.

## Context

keithmerrill.com is a two-page Next.js 16 site (App Router, React 19, Tailwind CSS v4,
react-three-fiber 9, three r183). TypeScript throughout; imports use the `@/` alias for
the repo root. The home page hero, `components/LSystemHero.tsx`, draws an L-system tree as
`lineSegments` inside an R3F `Canvas`, and grows it by widening the geometry's draw range
each frame. `lib/lsystem.ts` holds the grammar (`generate`) and the turtle (`interpret`,
which returns `Segment[]`, where `Segment = { start: Vector3; end: Vector3; depth: number }`).

**Defect 1: the hero never frees its geometry.** `Tree()` builds a `BufferGeometry` inside
a `useMemo` and passes it to `<lineSegments geometry={geometry}>`. react-three-fiber
disposes an unmounted object only when the object itself has a `dispose()` method, and a
`LineSegments` does not, so nothing ever calls `geometry.dispose()`. Today the Canvas
tears down its whole WebGL context on unmount, which hides this. It would leak as soon as
the tree is rebuilt inside a live Canvas.

**The fix:** move the geometry build into a pure function in a new module, and add a hook
that disposes the geometry when the component unmounts or its segments change. The pure
function and the hook are what the tests exercise.

**Defect 2: dead link code.** `data/projects.ts` declares a `ProjectLink` type and an
optional `links` field that no project fills in, and `components/ProjectCard.tsx` renders
a links list that therefore never appears. Remove all three.

**Defect 3: the template stylesheet overrides the site's design.** `app/globals.css` still
carries the Create Next App `:root` colour variables and a `body` rule setting
`background`, `color` and `font-family: Arial`. That rule is outside every CSS layer, and
unlayered rules beat Tailwind's layered utilities. So it overrides the root layout's
`bg-black text-zinc-100` on `<body>`, which makes the body white with dark text when the
visitor's OS is in light mode, and it replaces the Geist font that `app/layout.tsx` loads
with Arial. Nothing else uses those variables. Replace the file with the Geist font
mapping alone.

**Defect 4: the default favicon.** Add `app/icon.svg`, a small L-system mark in the hero's
colours. Next.js serves it as the site icon.

## Required change

### `lib/treeGeometry.ts` (new)

Exactly this content. The body of `buildTreeGeometry` is the code that sat in `Tree()`'s
`useMemo`, unchanged apart from the colour constants moving to the top.

```ts
import { useEffect, useMemo } from "react";
import { BufferAttribute, BufferGeometry, Color } from "three";
import type { Segment } from "./lsystem";

export const TRUNK_COLOR = "#3a4a2e";
export const TIP_COLOR = "#a9e070";

export type TreeGeometry = {
  geometry: BufferGeometry;
  totalVertices: number;
};

// Two vertices per segment. Colour runs from TRUNK_COLOR at depth 0 to
// TIP_COLOR at the deepest segment. The draw range starts empty; the hero
// grows it frame by frame.
export function buildTreeGeometry(segments: Segment[]): TreeGeometry {
  const positions = new Float32Array(segments.length * 6);
  const colors = new Float32Array(segments.length * 6);

  const trunkColor = new Color(TRUNK_COLOR);
  const tipColor = new Color(TIP_COLOR);
  const tmpColor = new Color();

  let maxDepth = 0;
  for (const seg of segments) {
    if (seg.depth > maxDepth) maxDepth = seg.depth;
  }
  const depthDivisor = Math.max(maxDepth, 1);

  segments.forEach((seg, i) => {
    const idx = i * 6;
    positions[idx + 0] = seg.start.x;
    positions[idx + 1] = seg.start.y;
    positions[idx + 2] = seg.start.z;
    positions[idx + 3] = seg.end.x;
    positions[idx + 4] = seg.end.y;
    positions[idx + 5] = seg.end.z;

    const t = seg.depth / depthDivisor;
    tmpColor.copy(trunkColor).lerp(tipColor, t);
    colors[idx + 0] = tmpColor.r;
    colors[idx + 1] = tmpColor.g;
    colors[idx + 2] = tmpColor.b;
    colors[idx + 3] = tmpColor.r;
    colors[idx + 4] = tmpColor.g;
    colors[idx + 5] = tmpColor.b;
  });

  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(positions, 3));
  geometry.setAttribute("color", new BufferAttribute(colors, 3));
  geometry.setDrawRange(0, 0);

  return { geometry, totalVertices: segments.length * 2 };
}

// react-three-fiber disposes an unmounted object only when the object itself
// has a dispose() method, and a LineSegments does not, so a geometry passed as
// a prop is never freed by R3F. The effect frees it when the component
// unmounts or the segments change.
export function useTreeGeometry(segments: Segment[]): TreeGeometry {
  const tree = useMemo(() => buildTreeGeometry(segments), [segments]);
  useEffect(() => () => tree.geometry.dispose(), [tree]);
  return tree;
}
```

The `import type` on `./lsystem` matters: see the test rules below.

### `components/LSystemHero.tsx` (modified: whole file)

Replace the whole file with exactly this. `Tree()` now memoizes the segments and takes its
geometry from the hook; the frame loop and `LSystemHero` do not change.

```tsx
"use client";

import { Canvas, useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import type { LineSegments as ThreeLineSegments } from "three";
import { defaultTreeConfig, generate, interpret } from "@/lib/lsystem";
import { useTreeGeometry } from "@/lib/treeGeometry";

function Tree() {
  const segments = useMemo(
    () => interpret(generate(defaultTreeConfig), defaultTreeConfig),
    [],
  );
  const { geometry, totalVertices } = useTreeGeometry(segments);

  const linesRef = useRef<ThreeLineSegments>(null);
  const startTimeRef = useRef<number | null>(null);
  const growDurationSeconds = 5.0;

  useFrame(({ clock }) => {
    if (startTimeRef.current === null) {
      startTimeRef.current = clock.elapsedTime;
    }
    const elapsed = clock.elapsedTime - startTimeRef.current;
    const t = Math.min(elapsed / growDurationSeconds, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    const visible = Math.floor(totalVertices * eased);
    geometry.setDrawRange(0, visible - (visible % 2));

    if (linesRef.current) {
      linesRef.current.rotation.y = clock.elapsedTime * 0.12;
    }
  });

  return (
    <lineSegments ref={linesRef} geometry={geometry} position={[0, -1.5, 0]}>
      <lineBasicMaterial vertexColors transparent opacity={0.9} />
    </lineSegments>
  );
}

export function LSystemHero() {
  return (
    <Canvas
      camera={{ position: [0, 0.8, 4], fov: 50 }}
      dpr={[1, 2]}
      gl={{ antialias: true }}
    >
      <color attach="background" args={["#0a0c0f"]} />
      <fog attach="fog" args={["#0a0c0f", 4, 10]} />
      <Tree />
    </Canvas>
  );
}
```

### `data/projects.ts` (modified: two anchored removals)

1. Delete these five lines, which open the file (the four-line type and the blank line
after it):

```ts
export type ProjectLink = {
  label: string;
  url: string;
};

```

2. Inside `export type Project`, delete the line

```ts
  links?: ProjectLink[];
```

Nothing else in the file changes. The `projects` array is long; do not rewrite it.

### `components/ProjectCard.tsx` (modified: one anchored removal)

Delete this block, which comes after the stack list and just before `    </article>`:

```tsx
      {project.links && project.links.length > 0 && (
        <ul className="mt-4 flex flex-wrap gap-3 font-mono text-xs">
          {project.links.map((l) => (
            <li key={l.url}>
              <a
                href={l.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-emerald-300/80 hover:text-emerald-200"
              >
                {l.label} →
              </a>
            </li>
          ))}
        </ul>
      )}
```

Nothing else in the file changes.

### `app/globals.css` (modified: whole file)

Replace the whole file with exactly this:

```css
@import "tailwindcss";

@theme inline {
  --font-sans: var(--font-geist-sans);
  --font-mono: var(--font-geist-mono);
}
```

Tailwind's base layer sets `html` to `--font-sans`, so Geist becomes the page font with
no `body` rule at all. Do not add one back.

### `app/icon.svg` (new)

Exactly this content:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="6" fill="#0a0c0f"/>
  <g fill="none" stroke="#a9e070" stroke-width="2.5" stroke-linecap="round">
    <path d="M16 28V16"/>
    <path d="M16 16L9 9M16 16L23 9"/>
    <path d="M9 9L8 4M9 9L4 7M23 9L24 4M23 9L28 7"/>
  </g>
</svg>
```

### `package.json` (modified: whole file)

Replace the whole file with exactly this. It adds a `test` script and four
devDependencies; every other entry is unchanged.

```json
{
  "name": "keithmerrill.com",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "eslint",
    "test": "vitest run"
  },
  "dependencies": {
    "@react-three/drei": "^10.7.7",
    "@react-three/fiber": "^9.6.0",
    "next": "16.2.3",
    "react": "19.2.4",
    "react-dom": "19.2.4",
    "three": "^0.183.2"
  },
  "devDependencies": {
    "@tailwindcss/postcss": "^4",
    "@testing-library/dom": "^10.4.2",
    "@testing-library/react": "^16.3.3",
    "@types/node": "^20",
    "@types/react": "^19",
    "@types/react-dom": "^19",
    "@types/three": "^0.183.1",
    "eslint": "^9",
    "eslint-config-next": "16.2.3",
    "jsdom": "^27.4.0",
    "tailwindcss": "^4",
    "typescript": "^5",
    "vitest": "^3.2.7"
  }
}
```

The four version ranges are not interchangeable with newer majors; see Risks.

### `README.md` (modified: two anchored edits)

1. In the Development code block, replace the line `npm run lint` with the two lines

```text
npm run lint
npm test        # vitest unit tests
```

2. In the Layout table, find the row whose first cell is `` `lib/lsystem.ts` ``. Change
its description from `L-System grammar and geometry generation` to
`L-System grammar and turtle interpretation`, trimming trailing spaces so the row's
closing `|` stays in the same column. Directly below it, add a row whose first cell is
`` `lib/treeGeometry.ts` `` and whose description is
`Hero line geometry, freed when the hero unmounts`, padded with spaces so both of its
inner `|` separators line up with the row above.

Nothing else in the README changes. (This edit is described rather than quoted because
the pipeline reads every line that starts with `|` as a change-surface row.)

### `lib/treeGeometry.test.ts` (new)

A vitest file. Its first line is exactly

```ts
// @vitest-environment jsdom
```

and it imports only from these four modules:

```ts
import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { Color, Vector3 } from "three";
import { TIP_COLOR, TRUNK_COLOR, buildTreeGeometry, useTreeGeometry } from "./treeGeometry";
```

**The sandbox rules.** The tests run in a sandbox that holds only the files in the change
surface plus the installed `node_modules`. Each rule below exists because breaking it makes
the suite fail to load:

- **Never import `./lsystem`, `@/…`, or any component.** `lib/lsystem.ts` is not in the
  sandbox, and there is no alias config. `treeGeometry.ts` imports `Segment` with
  `import type`, which the compiler erases, so it loads without `lsystem.ts`.
- **Build segments by hand** as `{ start: new Vector3(x, y, z), end: new Vector3(x, y, z),
  depth }`.
- **Add no config file.** No `vitest.config.*`, `tsconfig` change, or setup file; the
  first-line comment selects jsdom for this file.
- **Count disposal with three's event:**
  `geometry.addEventListener("dispose", () => { count += 1; })`. `dispose()` on a
  `BufferGeometry` dispatches that event, and it is the only observable effect.
- **Compare colours against `new Color(TRUNK_COLOR)` / `new Color(TIP_COLOR)`** with
  `toBeCloseTo(value, 5)`, never against hand-written floats: three converts sRGB hex to
  linear, so the stored values are not `0x3a / 255`.

The criteria below name the cases. `twoSegments` means
`[{start (0,0,0), end (0,1,0), depth 0}, {start (0,1,0), end (0.5,2,0.25), depth 2}]`.

## Change surface

The six modified files were verified to exist at `main`, and the three new ones
(`lib/treeGeometry.ts`, `lib/treeGeometry.test.ts`, `app/icon.svg`) were verified NOT to
exist. The plan's implement phase lists exactly these nine as outputs.

| Path | Change |
| --- | --- |
| `lib/treeGeometry.ts` | new |
| `lib/treeGeometry.test.ts` | new |
| `components/LSystemHero.tsx` | modified (whole file) |
| `data/projects.ts` | modified (two anchored removals) |
| `components/ProjectCard.tsx` | modified (one anchored removal) |
| `app/globals.css` | modified (whole file) |
| `app/icon.svg` | new |
| `package.json` | modified (whole file) |
| `README.md` | modified (two anchored edits) |

Each anchor quoted above occurs exactly once in its file at `main`. The protected files
below are served read-only. Do not edit them.

**Not in this spec.** The pipeline cannot delete files, so the operator deletes these by
hand when merging: `app/favicon.ico` (otherwise Next.js serves it beside `icon.svg`) and
the five unused template images `public/file.svg`, `public/globe.svg`, `public/next.svg`,
`public/vercel.svg`, `public/window.svg`. The operator also runs `npm install` to refresh
`package-lock.json`, which this spec must not write.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file fails
to load, and the report must say so.

1. **Positions.** `buildTreeGeometry(twoSegments)` returns `totalVertices` 4, and its
   `position` attribute has `count` 4, `itemSize` 3, and array
   `[0, 0, 0, 0, 1, 0, 0, 1, 0, 0.5, 2, 0.25]`.
2. **Empty draw range.** That geometry's `drawRange` has `start` 0 and `count` 0.
3. **Trunk-to-tip colour.** In that geometry's `color` attribute, vertices 0 and 1 (the
   depth-0 segment) equal `TRUNK_COLOR`, and vertices 2 and 3 (the deepest segment) equal
   `TIP_COLOR`, each channel to 5 decimal places.
4. **A lone trunk segment.** For a single depth-0 segment, both vertices equal
   `TRUNK_COLOR` (the depth divisor never reaches zero).
5. **No segments.** `buildTreeGeometry([])` returns `totalVertices` 0 and a `position`
   attribute with `count` 0.
6. **Disposed on unmount, once.** `renderHook(() => useTreeGeometry(twoSegments))`: the
   dispose count is 0 while mounted and exactly 1 after `unmount()`.
7. **Stable while the segments are unchanged.** After `rerender()` with the same array,
   `result.current.geometry` is the same object and the dispose count is 0.
8. **Old geometry disposed on change.** Render with `initialProps: { segments:
   twoSegments }`, then `rerender({ segments: [one depth-0 segment] })`: the geometry is a
   new object with `totalVertices` 2, and the first geometry's dispose count is 1. After
   `unmount()`, the second geometry's count is 1 and the first's is still 1.
9. **The whole suite passes** under `vitest run`.

## test_strategy

- framework: vitest
- required: true
- repo: keithmerrill-com
- base_ref: main
- protected_paths:
  - lib/lsystem.ts
  - package-lock.json
  - app/layout.tsx
  - app/page.tsx
  - app/projects/page.tsx
  - components/LSystemHeroClient.tsx
  - components/Nav.tsx
  - tsconfig.json
  - next.config.ts

## Constraints

- TypeScript only, in the repo's existing style: double quotes, semicolons, two-space
  indent, trailing commas in multi-line literals.
- No new runtime dependencies. The four devDependencies above are the only additions.
- No `vitest.config.*`, `vite.config.*` or setup files, and no `package-lock.json`.
- `lib/treeGeometry.ts` imports `Segment` with `import type`, and imports nothing else from
  `./lsystem`.
- `buildTreeGeometry` output must match the old inline code byte for byte (same arrays,
  same colours, same empty draw range), so the hero renders exactly as before.
- The frame loop in `Tree()` does not change.

## Risks

- **Newer vitest majors break this repo's install.** vitest 5 declares a peer
  `@types/node` of `^22 || >=24` and the site pins `^20`, so `npm install` fails with
  ERESOLVE. vitest 4 crashes npm 10.9's resolver outright (`Cannot read properties of null
  (reading 'edgesOut')`) against this dependency set, with vite 6, 7 or 8. vitest `^3.2.7`
  installs cleanly. Keep `^3.2.7`.
- **jsdom 28 and later need Node ≥ 22.22.2**; the sandbox runs Node 22.21.1. Keep
  `^27.4.0`.
- **Importing `lsystem` at runtime.** `import { Segment } from "./lsystem"` (without
  `type`), or a test that calls `interpret`, makes the suite fail to load: the sandbox has
  no `lib/lsystem.ts`.
- **Disposing inside `useMemo`.** Disposal belongs in the effect's cleanup. A dispose
  anywhere else either never runs on unmount (criterion 6) or frees a geometry that is
  still drawn.
- **`@testing-library/react` without `@testing-library/dom`.** RTL 16 lists `dom` as a
  peer, and without it `renderHook` fails to import.
- **Rewriting `data/projects.ts`.** The two removals are the whole change; the project
  entries are the site's content and must come through untouched.
