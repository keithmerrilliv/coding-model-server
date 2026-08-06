"""Wall-clock and token cost per attempt, by agent and role (DEV-528).

This is the query the telemetry audit could not run. Before DEV-528 an
`agent_ran` event carried no duration and no token counts, and named its
agent in only 54% of cases, so "which agent is cheapest per attempt" had no
answer — and neither did "is this agent slow or just unlucky".

    venv/bin/python scripts/agent_cost_stats.py
    venv/bin/python scripts/agent_cost_stats.py --since 2026-08-06
    venv/bin/python scripts/agent_cost_stats.py --role implementer
    venv/bin/python scripts/agent_cost_stats.py --spec spec_1b927743

Two things this deliberately does NOT do:

  * It does not pool anomaly and routing records with real model calls.
    Those set `model_call: False` and are counted separately, because an
    attempt that never called a model costs nothing and would drag every
    median toward zero.
  * It does not hide rows it cannot attribute. Events predating DEV-528 have
    no telemetry at all, and a per-agent table that quietly omits them looks
    far more complete than it is — the coverage line is printed every run.

Reading the output: `calls` is model calls, `n` is attempts. In manifest mode
one implementer attempt is 1 + N calls summed into a single event, so a large
calls/n ratio is normal there and is the reason per-attempt is the unit.

CAVEAT, and it is not a small one: per-agent rates from production runs are
confounded by construction, because rotation assigns a later agent only after
an earlier one failed. See DEV-530 before ranking anything by these numbers.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "var" / "tasks_db" / "tasks.sqlite"


def _load(db_path: Path, since: Optional[str], until: Optional[str],
          spec_id: Optional[str], role: Optional[str]) -> list[dict]:
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Read-only: this runs against the live orchestrator DB while a spec may
    # be executing, and a stats script must never take a write lock on it.
    sql = ("SELECT spec_id, payload_json, created_at FROM events "
           "WHERE LOWER(kind) = 'agent_ran' AND payload_json IS NOT NULL")
    args: list = []
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    if until:
        sql += " AND created_at <= ?"
        args.append(until)
    if spec_id:
        sql += " AND spec_id = ?"
        args.append(spec_id)

    out = []
    for row in conn.execute(sql, args):
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if role and payload.get("role") != role:
            continue
        payload["_spec_id"] = row["spec_id"]
        out.append(payload)
    return out


def _fmt_ms(ms: float) -> str:
    if ms >= 60_000:
        return f"{ms / 60_000:.1f}m"
    return f"{ms / 1000:.1f}s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--since", help="ISO date/timestamp, inclusive")
    ap.add_argument("--until", help="ISO date/timestamp, inclusive")
    ap.add_argument("--spec", dest="spec_id")
    ap.add_argument("--role", help="restrict to one role, e.g. implementer")
    args = ap.parse_args()

    events = _load(args.db, args.since, args.until, args.spec_id, args.role)
    if not events:
        print("no agent_ran events matched")
        return

    bookkeeping = [e for e in events if e.get("model_call") is False]
    model_calls = [e for e in events if e.get("model_call") is not False]
    timed = [e for e in model_calls if e.get("duration_ms") is not None]
    untimed = len(model_calls) - len(timed)

    groups: dict[tuple, list] = defaultdict(list)
    for e in timed:
        groups[(e.get("agent") or "(unattributed)", e.get("role") or "?")].append(e)

    print(f"{'agent':<24} {'role':<22} {'n':>4} {'calls':>6} "
          f"{'med':>7} {'p95':>7} {'med tok':>8}")
    print("-" * 82)
    for (agent, role_name), rows in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        durs = sorted(r["duration_ms"] for r in rows)
        toks = sorted(r["total_tokens"] for r in rows
                      if r.get("total_tokens") is not None)
        calls = sum(r.get("calls", 1) for r in rows)
        # p95 by nearest rank — with n in the tens, interpolation would invent
        # precision the sample does not carry.
        p95 = durs[min(len(durs) - 1, int(round(0.95 * len(durs))) - 1)]
        med_tok = f"{int(statistics.median(toks)):,}" if toks else "-"
        print(f"{agent:<24} {role_name:<22} {len(rows):>4} {calls:>6} "
              f"{_fmt_ms(statistics.median(durs)):>7} {_fmt_ms(p95):>7} "
              f"{med_tok:>8}")

    print()
    print(f"attempts with telemetry: {len(timed)} of {len(model_calls)} "
          f"model calls ({100 * len(timed) / len(model_calls):.0f}%)")
    if untimed:
        print(f"  {untimed} without duration — events predating DEV-528, "
              "or a call that raised before returning")
    if bookkeeping:
        print(f"  {len(bookkeeping)} anomaly/routing records excluded "
              "(model_call: false — no model ran)")
    print("per-agent rates are confounded by rotation order — see DEV-530")


if __name__ == "__main__":
    main()
