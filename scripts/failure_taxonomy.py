#!/usr/bin/env python3
"""Failures by class, by agent, by retry index — the query DEV-529 asked for.

Reads the failure_classified stream (DEV-629) that outcome.dispose writes for
every failed attempt. Each verdict row carries its diagnostics, their closed-
set classes (DEV-529), the files and symbols they name, the agent that
produced it (DEV-631) and the retry index. Nothing here needs the daemon.

    python scripts/failure_taxonomy.py                 # everything
    python scripts/failure_taxonomy.py --since 2026-09-01 --by agent
    python scripts/failure_taxonomy.py --spec spec_f7df9ce0 --rows

The per-agent table is confounded by rotation position (DEV-530): read it
beside `assignment` and `prior_cls` on the attempt_planned rows, never alone.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.getenv("CODING_MODEL_TASKS_DB",
                            REPO / "var" / "tasks_db" / "tasks.sqlite"))


def rows(db: Path, *, since: str | None, until: str | None, spec: str | None):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sql = "SELECT spec_id, task_id, created_at, payload_json FROM events WHERE kind = 'failure_classified'"
    args: list = []
    if since:
        sql += " AND created_at >= ?"; args.append(since)
    if until:
        sql += " AND created_at < ?"; args.append(until)
    if spec:
        sql += " AND spec_id = ?"; args.append(spec)
    sql += " ORDER BY id"
    for spec_id, task_id, created_at, payload_json in conn.execute(sql, args):
        try:
            p = json.loads(payload_json or "{}")
        except ValueError:
            continue
        yield {"spec": spec_id, "task": task_id, "at": created_at, **p}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--since", help="ISO date/time, inclusive")
    ap.add_argument("--until", help="ISO date/time, exclusive")
    ap.add_argument("--spec")
    ap.add_argument("--by", choices=("class", "agent", "retry", "cls"), default="class",
                    help="class = diagnostic class (DEV-529); cls = failure class (DEV-629)")
    ap.add_argument("--verdicts-only", action="store_true",
                    help="drop no-verdict rows (transport, truncation, ...)")
    ap.add_argument("--rows", action="store_true", help="print every row instead of a table")
    a = ap.parse_args(argv)
    if not a.db.is_file():
        print(f"no database at {a.db}", file=sys.stderr)
        return 2

    data = list(rows(a.db, since=a.since, until=a.until, spec=a.spec))
    if a.verdicts_only:
        data = [r for r in data if r.get("outcome") == "verdict"]
    if a.rows:
        for r in data:
            print(f"{r['at']}  {r['spec']}  retry={r.get('retry')}  agent={r.get('agent') or '?':<18} "
                  f"{r.get('cls'):<20} {','.join(r.get('diagnostic_classes') or []) or '-':<28} "
                  f"{' '.join(r.get('cited_files') or [])[:60]}")
        return 0

    table: dict = collections.defaultdict(collections.Counter)
    for r in data:
        if a.by == "class":
            keys = r.get("diagnostic_classes") or (["(no diagnostics)"] if r.get("outcome") == "verdict" else ["(no verdict)"])
            for k in keys:
                table[k][r.get("agent") or "?"] += 1
        elif a.by == "agent":
            for k in r.get("diagnostic_classes") or ["(none)"]:
                table[r.get("agent") or "?"][k] += 1
        elif a.by == "retry":
            for k in r.get("diagnostic_classes") or ["(none)"]:
                table[f"retry {r.get('retry')}"][k] += 1
        else:
            table[r.get("cls")][r.get("agent") or "?"] += 1
    cols = sorted({c for counts in table.values() for c in counts})
    head = f"{'':<24}" + "".join(f"{c[:16]:>18}" for c in cols) + f"{'total':>8}"
    print(head)
    for k in sorted(table, key=lambda k: -sum(table[k].values())):
        line = f"{str(k)[:24]:<24}" + "".join(f"{table[k].get(c, 0):>18}" for c in cols)
        print(line + f"{sum(table[k].values()):>8}")
    print(f"\n{len(data)} classified failure(s)" + (f" since {a.since}" if a.since else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
