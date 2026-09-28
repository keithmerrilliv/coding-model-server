"""Language packs: what the pipeline knows about each language, behind one
interface (:class:`~.base.LanguagePack`).

This package is the one place that decides what language a file, a file set
or a design is in. Everything outside it asks here, then calls the pack's
hooks: standing prompt rules, operator-switched target rules, compiler fix
hints, prechecks, declared types, test counting, normalizers, and the design
rules a language opts into. The kernel and the daemon name no language, so
supporting a new one is a new pack in this package and nothing else.

Test-OUTPUT parsing is deliberately not here. It is keyed by test framework,
not language (one language has jest, vitest and node_test), and it lives in
``diagnostics``, a leaf module that owns those patterns.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .base import (  # noqa: F401  (re-exported: the interface's names)
    RULE_COMPLETENESS,
    RULE_EQUATABLE,
    RULE_SEAM_IMPORTS,
    RULE_TUPLE_CONFORMANCE,
    LanguagePack,
    PrecheckResult,
)
from .c_family import CFamilyPack
from .javascript import JavaScriptPack
from .python import PythonPack
from .swift import SwiftPack

#: Every pack, keyed by name. Hooks that combine packs run in this order.
PACKS: dict[str, LanguagePack] = {
    p.name: p for p in (SwiftPack(), PythonPack(), JavaScriptPack(), CFamilyPack())
}

# (extension, language, pack), in precedence order for language_from_paths:
# Objective-C++ is the most specific claim a change surface can make, then
# Objective-C, then Swift. A language is the name retrieval and the plan use;
# a pack is the code that knows it, and one pack can serve several languages.
# None for the language means the extension decides none on its own: a `.h`
# could be C, C++ or Objective-C.
_EXTENSIONS: tuple[tuple[str, Optional[str], Optional[str]], ...] = (
    (".mm", "objective-c++", "c_family"),
    (".m", "objective-c", "c_family"),
    (".swift", "swift", "swift"),
    (".py", "python", "python"),
    (".ts", "typescript", "javascript"), (".tsx", "typescript", "javascript"),
    (".js", "javascript", "javascript"), (".jsx", "javascript", "javascript"),
    (".rs", "rust", None), (".go", "go", None), (".kt", "kotlin", None),
    (".java", "java", None),
    (".cpp", "c++", "c_family"), (".cc", "c++", "c_family"),
    (".cxx", "c++", "c_family"), (".c", "c", "c_family"),
    (".hpp", None, "c_family"), (".hh", None, "c_family"), (".h", None, "c_family"),
)
_PACK_OF_EXTENSION = {ext: pack for ext, _, pack in _EXTENSIONS if pack}

# An extension a pack owns, where it appears in prose (a design's File
# Structure). The trailing \b keeps `.m` from matching `.metal` or `.md`.
_PACK_EXTENSION_IN_TEXT_RE = re.compile(
    r"\.(" + "|".join(sorted((e[1:] for e in _PACK_OF_EXTENSION),
                             key=len, reverse=True)) + r")\b")

# DEV-781: one spelling per language, so the planner's choice of "objc",
# "Obj-C" or "Objective-C" cannot switch retrieval off. Unknown names pass
# through lowercased, and the retrieval gate says "language_not_covered".
_LANGUAGE_ALIASES = {
    "objc": "objective-c", "obj-c": "objective-c", "objectivec": "objective-c",
    "objective c": "objective-c", "objective-c": "objective-c",
    "objc++": "objective-c++", "obj-c++": "objective-c++",
    "objcpp": "objective-c++", "objective c++": "objective-c++",
    "objective-c++": "objective-c++", "objectivec++": "objective-c++",
    "py": "python", "swiftui": "swift",
}


def normalize_language(name: "str | None") -> "str | None":
    """Canonical lowercase language name, or None for empty input."""
    if name is None:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    return _LANGUAGE_ALIASES.get(key, key)


def _extension(path: str) -> str:
    return os.path.splitext(str(path))[1].lower()


def language_from_paths(paths: "list[str] | None") -> "str | None":
    """The language a set of file paths implies (DEV-781), or None.

    The change surface is the one deterministic signal for Objective-C++:
    a `.mm` file is Objective-C++ by definition, where the prose and the
    planner's guess are not. Used only when the plan does not say.
    """
    exts = {_extension(p) for p in (paths or [])}
    for ext, lang, _ in _EXTENSIONS:
        if lang and ext in exts:
            return lang
    return None


def pack_for_path(path: str) -> Optional[LanguagePack]:
    """The pack that owns a file, by its extension."""
    name = _PACK_OF_EXTENSION.get(_extension(path))
    return PACKS[name] if name else None


def packs_for_paths(paths: "list[str] | None") -> list[LanguagePack]:
    """The packs a file set touches, in registry order."""
    names = {_PACK_OF_EXTENSION.get(_extension(p)) for p in (paths or [])}
    return [p for n, p in PACKS.items() if n in names]


def packs_in_text(text: str) -> list[LanguagePack]:
    """The packs whose file extensions a piece of prose mentions."""
    names = {_PACK_OF_EXTENSION["." + m.lower()]
             for m in _PACK_EXTENSION_IN_TEXT_RE.findall(text or "")}
    return [p for n, p in PACKS.items() if n in names]


def rule_applies(text: str, rule: str) -> bool:
    """True when the files *text* allocates are all in languages that opt
    into *rule*. A design that names no known language gets no
    language-specific rule: silence is the safe side of a design check."""
    packs = packs_in_text(text)
    return bool(packs) and all(rule in p.design_rules for p in packs)


def pack_for_framework(framework: "str | None") -> Optional[LanguagePack]:
    """The pack whose code a test framework builds and tests."""
    fw = str(framework or "").lower()
    return next((p for p in PACKS.values() if fw in p.frameworks), None)


def render_rules(paths: "list[str]") -> str:
    """Every standing rules section the file set's languages carry, or ""
    so a prompt without them stays byte-identical."""
    return "".join(p.standing_rules(paths) for p in packs_for_paths(paths))


def render_target_rules(test_strategy: "dict | None") -> str:
    """Every rules section the operator's test_strategy switches on."""
    ts = test_strategy if isinstance(test_strategy, dict) else {}
    return "".join(p.target_rules(ts) for p in PACKS.values())


def fix_hint(message: str) -> Optional[str]:
    """The one edit a compiler diagnostic asks for, from whichever pack
    recognises it, or None."""
    for p in PACKS.values():
        hint = p.fix_hint(message)
        if hint:
            return hint
    return None


def declared_types(path: str, content: str) -> set[str]:
    """Type names *content* declares at file scope, per its language."""
    pack = pack_for_path(path)
    return pack.declared_types(content) if pack else set()


def normalize_file(path: str, content: str) -> tuple[str, list[str]]:
    """Every pack's deterministic fix applied to one file: the new content
    and a note per change."""
    notes: list[str] = []
    for p in PACKS.values():
        content, note = p.normalize(path, content)
        if note:
            notes.append(note)
    return content, notes
