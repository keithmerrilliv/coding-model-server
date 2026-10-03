"""The JavaScript and TypeScript pack: exact-pinned package.json ranges."""
from __future__ import annotations

import json
import os
from typing import Optional

from .base import LanguagePack


# ── Deterministic boilerplate normalization (#3) ─────────────────────────────
#
# Some boilerplate the reviewer checks is fully deterministic — there is exactly
# one correct form — so it should not ride on stochastic generation. The reviewer
# rejects unpinned dependencies (`^`, `~`, `>=` ranges) on sight, and that is the
# single most common, most mechanical FAIL. We pin them ourselves after
# generation rather than hoping every implementer model gets it right every time.

def pin_version(spec: str) -> str:
    """Pin a single dependency version spec to an exact version.

    `^1.2.3` / `~1.2.3` / `>=1.2.3` → `1.2.3`. Leaves already-exact versions,
    and non-semver specs (URLs, `workspace:*`, `*`, git refs) untouched.
    """
    s = spec.strip()
    if s[:1] in "^~><=":
        stripped = s.lstrip("^~><= ")
        if stripped[:1].isdigit():  # only pin when a concrete version remains
            return stripped
    return s


def _pin_package_json(content: str) -> tuple[str, int]:
    try:
        data = json.loads(content)
    except ValueError:
        return content, 0  # not valid JSON — leave it for the reviewer to flag
    if not isinstance(data, dict):
        return content, 0
    changed = 0
    for key in ("dependencies", "devDependencies",
                "peerDependencies", "optionalDependencies"):
        deps = data.get(key)
        if not isinstance(deps, dict):
            continue
        for name, ver in list(deps.items()):
            if isinstance(ver, str):
                pinned = pin_version(ver)
                if pinned != ver:
                    deps[name] = pinned
                    changed += 1
    if changed == 0:
        return content, 0
    return json.dumps(data, indent=2) + "\n", changed


class JavaScriptPack(LanguagePack):
    name = "javascript"
    frameworks = frozenset({"jest", "vitest", "node_test"})
    # A package-lock.json is routinely the largest file in a repository: one
    # of 252,037 characters, served as protected context, was by itself more
    # than every implementer window but the largest could hold. package.json
    # is NOT here — it is hand-written and names the scripts and dependencies
    # the code relies on.
    generated_files = frozenset({
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml", "bun.lock", "bun.lockb", "deno.lock",
    })
    # Minified bundles and source maps: build output, one unreadable line each.
    # Source maps are matched with the extension they map, not as a bare
    # `.map` — a game's `level1.map` is data a person wrote.
    generated_suffixes = (".min.js", ".min.mjs", ".min.cjs", ".min.css",
                          ".js.map", ".mjs.map", ".cjs.map", ".css.map")

    def normalize(self, path: str, content: str) -> tuple[str, Optional[str]]:
        if os.path.basename(path) != "package.json":
            return content, None
        new_content, changed = _pin_package_json(content)
        if not changed:
            return content, None
        return new_content, (f"{path}: pinned {changed} dependency range(s) "
                             f"to exact versions")
