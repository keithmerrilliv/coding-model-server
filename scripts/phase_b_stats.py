"""Aggregate stats for Phase b (adversarial test generation via Gemini).

Queries the orchestrator's events table for adversarial_test_writer events
and prints counters useful for tuning the false-FAIL rate before promoting
the feature from opt-in.

Run after a validation batch:

    venv/bin/python scripts/phase_b_stats.py
    venv/bin/python scripts/phase_b_stats.py --since 2026-05-01
    venv/bin/python scripts/phase_b_stats.py --spec spec_3b64bc43

Outputs counts only; spec-level inspection is via `EventTimeline` in the
per-spec dashboard view (events with role=adversarial_test_writer show
up there automatically).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "qwen_tasks_db" / "tasks.sqlite"


def _query_events(db_path: Path, since: Optional[str], spec_id: Optional[str]) -> list[dict]:
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, spec_id, task_id, kind, payload_json, created_at FROM events WHERE kind = 'AGENT_RAN'"
    args: list = []
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    if spec_id:
        sql += " AND spec_id = ?"
        args.append(spec_id)
    sql += " ORDER BY id ASC"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except json.JSONDecodeError:
            continue
        if payload.get("role") != "adversarial_test_writer":
            continue
        out.append({
            "id": r["id"],
            "spec_id": r["spec_id"],
            "task_id": r["task_id"],
            "created_at": r["created_at"],
            "model": payload.get("model"),
            "tests_added": payload.get("tests_added"),
            "passed": payload.get("passed"),
            "skip_reason": payload.get("skip_reason"),
        })
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB),
                   help=f"path to orchestrator events DB (default: {DEFAULT_DB})")
    p.add_argument("--since", help="ISO date/timestamp filter, e.g. 2026-05-01")
    p.add_argument("--spec", help="restrict to one spec_id")
    args = p.parse_args()

    events = _query_events(Path(args.db), args.since, args.spec)
    if not events:
        print("No phase-b events match the filter.")
        return

    fired = len(events)
    skipped_no_blocks = sum(1 for e in events if e["skip_reason"] == "no_blocks_returned")
    wrote_tests = [e for e in events if e["tests_added"] and e["tests_added"] > 0]
    passed_after_phase_b = sum(1 for e in wrote_tests if e["passed"] is True)
    failed_after_phase_b = sum(1 for e in wrote_tests if e["passed"] is False)
    test_count_total = sum(e["tests_added"] or 0 for e in wrote_tests)
    specs_touched = len({e["spec_id"] for e in events})
    models = Counter(e["model"] for e in events if e["model"])

    print(f"Phase b stats — {fired} firing(s) across {specs_touched} spec(s)")
    if args.since:
        print(f"  since: {args.since}")
    if args.spec:
        print(f"  spec:  {args.spec}")
    print()
    print(f"  Skipped (Gemini decided no new tests):  {skipped_no_blocks}")
    print(f"  Wrote tests:                            {len(wrote_tests)}")
    print(f"    Tests written total:                  {test_count_total}")
    print(f"    Combined run PASSED (PASS stands):    {passed_after_phase_b}")
    print(f"    Combined run FAILED (retry forced):   {failed_after_phase_b}")
    print()
    if models:
        print("  Models:")
        for model, n in models.most_common():
            print(f"    {model}: {n}")
        print()

    if wrote_tests:
        catch_rate = failed_after_phase_b / len(wrote_tests)
        print(f"  Catch-or-overspecify rate (failed / wrote_tests): {catch_rate:.0%}")
        print(f"    Inspect the failed ones case-by-case to classify "
              f"as real catch vs over-specification.")


if __name__ == "__main__":
    main()
