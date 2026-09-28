"""Cited diagnostics in the repair and retry prompts, and cite-or-refuse
(DEV-767, DEV-778).

A build's ``path:line`` citations (located by ``diagnostics``) are rendered
into the prompt with the one fix each asks for, from whichever language pack
recognises the message. A synthesis repair may then only replace files a
diagnostic names, and is not worth a build round trip unless it changed at
least one cited line.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from . import languages
from .diagnostics import LocatedDiagnostic


def render_cited_diagnostics(diags: "list[LocatedDiagnostic]",
                             *, repair: bool = True) -> str:
    """Every cited location with its hint (DEV-778: rendered ONLY for the
    diagnostics actually present), followed by the rule for this prompt.

    ``repair=True`` is the synthesis repair round (DEV-767): edits must land
    on cited files and lines or the repair is refused. ``repair=False`` is
    the implementer's build-failure retry, where whole-file re-emission is
    the contract and other files are legal, so the rule is softer: change
    the named lines, and nothing a diagnostic does not require.
    """
    if not diags:
        return ""
    lines = ["## Cited locations — your edits MUST land here\n\n"]
    for d in diags[:40]:
        hint = languages.fix_hint(d.message)
        lines.append(f"- `{d.located()}`: {d.message}")
        if hint:
            lines.append(f"  → fix: {hint}")
        lines.append("")
    if repair:
        lines.append(
            "Emit a <<<FILE: path>>> block ONLY for files listed above, and make "
            "sure each block changes at least one of its cited lines. A block for "
            "any other file is discarded before the build; a repair that changes "
            "no cited line is not built at all.\n\n"
        )
    else:
        lines.append(
            "Each `→ fix` is the ONE edit that diagnostic asks for; apply it on "
            "the line named (or on the declaration it points at). Do not "
            "rewrite lines no diagnostic names, and do not restructure a "
            "function to avoid a one-token fix.\n\n"
        )
    return "\n".join(lines)


# ── Cite-or-refuse ────────────────────────────────────────────────────────

@dataclass
class CiteFilterResult:
    kept: "list[tuple[str, str]]"
    dropped: "list[str]" = field(default_factory=list)      # uncited paths
    untouched: "list[str]" = field(default_factory=list)    # cited, no cited line changed
    touched_cited_line: bool = False
    applied: bool = False   # False when there was nothing to cite against

    def refuse(self) -> bool:
        """True when the repair is not worth building."""
        return self.applied and not self.touched_cited_line


def _changed_line_indices(before: str, after: str) -> set:
    """0-based indices of *before* lines that a diff replaces or deletes,
    plus the neighbours of pure insertions (an insertion between lines
    i-1 and i touches both)."""
    a = before.splitlines()
    b = after.splitlines()
    changed: set = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            changed.update({i1 - 1, i1})
        else:
            changed.update(range(i1, i2))
    return changed


def filter_repair_to_cited(repair_files: "list[tuple[str, str]]",
                           before: "dict[str, str | None]",
                           diags: "list[LocatedDiagnostic]") -> CiteFilterResult:
    """Keep only the emitted files a diagnostic cites, and say whether any
    cited line changed.

    *before* maps each emitted relpath to its pre-repair content (None when
    the file did not exist). When no diagnostic maps onto an artifact the
    filter is a no-op (``applied=False``) — there is nothing to cite against,
    e.g. a near-miss test failure rather than a build failure.
    """
    cited: "dict[str, set]" = {}
    for d in diags:
        if d.artifact:
            cited.setdefault(d.artifact, set()).add(d.line - 1)  # 0-based
    if not cited:
        return CiteFilterResult(kept=list(repair_files), applied=False,
                                touched_cited_line=True)
    res = CiteFilterResult(kept=[], applied=True)
    for rel, content in repair_files:
        if rel not in cited:
            res.dropped.append(rel)
            continue
        prev = before.get(rel)
        if prev is None:
            # A cited file the workspace does not have: nothing to compare
            # against, keep it and count it as touched.
            res.kept.append((rel, content))
            res.touched_cited_line = True
            continue
        changed = _changed_line_indices(prev, content)
        # A cited line counts as touched if it, or an immediate neighbour,
        # changed — `throws` lands on the `func` line one above a cited `try`.
        hits = {i + off for i in cited[rel] for off in (-1, 0, 1)}
        if changed & hits:
            res.kept.append((rel, content))
            res.touched_cited_line = True
        else:
            res.kept.append((rel, content))
            res.untouched.append(rel)
    return res
