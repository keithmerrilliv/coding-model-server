#!/usr/bin/env python
"""Measure the local Swift prechecks against the spec archive (DEV-777).

Runs every detector over every Swift file set in var/tasks_db/specs — the spec
workspace itself and each retry_history/* directory — and reports, per
detector, how many sets it fired on, split by what the Mac said about that set:

  build_failed   the directory carries build_failure.txt (a real compiler
                 verdict exists; a hit here is a candidate TRUE positive)
  passed         test_output.txt / build_check_output.txt says the suite ran
                 green (a hit here is a FALSE positive by construction)
  unknown        neither — never built, or the record is gone

Run it before shipping a new detector (see the memory rule "guards must arm on
the spec archive") and paste the table on the ticket.

    ./venv/bin/python scripts/sweep_swift_prechecks.py [--show KIND] [--root DIR]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from coding_model_autonomous import swift_prechecks as sp  # noqa: E402

_GREEN = re.compile(r"TEST SUCCEEDED|Test run with \d+ tests? passed|"
                    r"Executed \d+ tests?, with 0 failures|\b\d+ passed\b")


def _verdict(d: Path) -> str:
    """The set's LAST word from the Mac, not its most flattering one.

    The outputs are ordered newest-authority first and the FIRST that exists
    decides. Falling through to an older one let a superseded green mask a
    later red: spec_7ff43f1f/retry_2's build check passed at 19:55, the
    reviewer's dispatch failed at 20:04 with `invalid redeclaration`, and the
    old loop skipped the red test_output.txt (it contains "error:") to report
    the stale green build_check_output.txt as `passed`. That turned a correct
    precheck hit into a phantom false positive in the table this script exists
    to produce.
    """
    if (d / "build_failure.txt").exists():
        return "build_failed"
    for name in ("test_output.txt", "build_check_output.txt"):
        f = d / name
        if not f.exists():
            continue
        txt = re.sub(r"\x1b\[[0-9;]*m", "", f.read_text(errors="replace"))
        if "error:" in txt:
            return "build_failed"
        return "passed" if _GREEN.search(txt) else "unknown"
    return "unknown"


def _swift_set(d: Path) -> list[tuple[str, str]]:
    out = []
    for p in d.rglob("*.swift"):
        rel = p.relative_to(d)
        if rel.parts and rel.parts[0] in ("retry_history", "_contained", ".repo_overlay"):
            continue
        out.append((str(rel), p.read_text(errors="replace")))
    return out


def _isolation_for(d: Path, root: Path) -> "str | None":
    """DEV-784: Electric Sheep's app target is default-MainActor; read the
    set's spec plan (the parent spec's for retry_history entries)."""
    spec = d if d.parent == root else d.parent.parent
    plan = spec / "plan.yaml"
    try:
        import yaml
        ts = (yaml.safe_load(plan.read_text()) or {}).get("test_strategy") or {}
    except Exception:
        return None
    if ts.get("default_actor_isolation"):
        return str(ts["default_actor_isolation"])
    return "MainActor" if ts.get("repo") == "electric-sheep" else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="var/tasks_db/specs")
    ap.add_argument("--show", help="print every hit of this detector kind")
    args = ap.parse_args()
    root = Path(args.root)
    dirs: list[Path] = []
    for spec in sorted(root.glob("spec_*")):
        dirs.append(spec)
        rh = spec / "retry_history"
        if rh.is_dir():
            dirs.extend(sorted(p for p in rh.iterdir() if p.is_dir()))
    sets = 0
    by_kind: dict[str, Counter] = defaultdict(Counter)
    hits: list[tuple[str, str, sp.Violation]] = []
    for d in dirs:
        files = _swift_set(d)
        if not files:
            continue
        sets += 1
        v = _verdict(d)
        result = sp.run_swift_prechecks(files, default_isolation=_isolation_for(d, root))
        for kind in {x.kind for x in result.violations}:
            by_kind[kind][v] += 1
        for x in result.violations:
            hits.append((str(d.relative_to(root)), v, x))
    print(f"swift file sets scanned: {sets}")
    print(f"{'detector':44} {'build_failed':>12} {'passed':>8} {'unknown':>8}")
    for kind in sorted(by_kind):
        c = by_kind[kind]
        print(f"{kind:44} {c['build_failed']:>12} {c['passed']:>8} {c['unknown']:>8}")
    if args.show:
        print()
        for where, v, x in hits:
            if x.kind == args.show:
                print(f"[{v}] {where}: {x.path}:{x.line}: {x.message[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
