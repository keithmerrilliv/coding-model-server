#!/usr/bin/env python
"""Inventory the open pipeline/* delivery branches of a target repo (DEV-756).

For every `origin/pipeline/*` branch: when it was delivered, the commit it was
cut from, whether that base is still an ancestor of the default branch, and —
the check that matters — which test functions the DEFAULT branch has that the
branch would remove if merged as-is. Three of four open Centipede branches
were pure deletions by the time anyone looked; nothing listed them.

    ./venv/bin/python scripts/pipeline_branches.py ~/Dev/Centipede [--main origin/main]

Read-only: it fetches, it never merges or deletes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from coding_model_autonomous.delivery import test_names  # noqa: E402


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--main", default="origin/main")
    args = ap.parse_args()
    repo = Path(args.repo).expanduser()
    git(repo, "fetch", "--prune", "origin")
    branches = [b.strip() for b in git(repo, "branch", "-r", "--list", "origin/pipeline/*").splitlines()]
    if not branches:
        print("no open pipeline/* branches")
        return 0
    print(f"{'branch':32} {'delivered':10} {'base':9} {'base in main':12} {'+/-':>9}  tests main has that the branch removes")
    for b in branches:
        date = git(repo, "log", "-1", "--format=%cs", b).strip()
        base = git(repo, "merge-base", args.main, b).strip()[:9]
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base, args.main],
                                  cwd=str(repo)).returncode == 0
        stat = git(repo, "diff", "--shortstat", f"{args.main}...{b}").strip() or "(no diff)"
        plus = minus = "0"
        for part in stat.split(","):
            part = part.strip()
            if part.endswith("(+)"):
                plus = part.split()[0]
            elif part.endswith("(-)"):
                minus = part.split()[0]
        lost: list[str] = []
        # Files the BRANCH changed since its base: a merge replays only those
        # over main, so only those can remove a test main has.
        files = git(repo, "diff", "--name-only", f"{base}..{b}").split()
        for f in files:
            if "Test" not in f:
                continue
            try:
                main_src = git(repo, "show", f"{args.main}:{f}")
            except SystemExit:
                continue  # file does not exist on main: nothing to lose
            try:
                branch_src = git(repo, "show", f"{b}:{f}")
            except SystemExit:
                branch_src = ""
            lost += sorted(test_names(main_src) - test_names(branch_src))
        flag = "" if not lost else f"{len(lost)}: " + ", ".join(lost[:6]) + (" …" if len(lost) > 6 else "")
        print(f"{b.replace('origin/', ''):32} {date:10} {base:9} {'yes' if ancestor else 'NO':12} {'+' + plus + '/-' + minus:>9}  {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
