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
    # `kind` is stored as the enum's lowercase value (e.g. 'agent_ran'),
    # not the Python enum name. LOWER() defends against any accidental
    # uppercase rows that might exist from older code paths.
    sql = "SELECT id, spec_id, task_id, kind, payload_json, created_at FROM events WHERE LOWER(kind) = 'agent_ran'"
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
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "tests_added": payload.get("tests_added"),
            "passed": payload.get("passed"),
            "error": payload.get("error"),
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

    specs_touched = len({e["spec_id"] for e in events})
    print(f"Phase b stats — {len(events)} provider firing(s) across {specs_touched} spec(s)")
    if args.since:
        print(f"  since: {args.since}")
    if args.spec:
        print(f"  spec:  {args.spec}")
    print()

    # Roll up by provider. Older events (pre multi-provider) have
    # provider=None; bucket them under "unknown" so they're still visible.
    provider_keys = sorted({e["provider"] or "unknown" for e in events})
    for provider in provider_keys:
        bucket = [e for e in events if (e["provider"] or "unknown") == provider]
        wrote = [e for e in bucket if (e["tests_added"] or 0) > 0]
        skipped = sum(1 for e in bucket if e["skip_reason"] == "no_blocks_returned")
        errored = sum(1 for e in bucket if e["error"])
        passed = sum(1 for e in wrote if e["passed"] is True)
        failed = sum(1 for e in wrote if e["passed"] is False)
        tests_total = sum(e["tests_added"] or 0 for e in wrote)
        models = Counter(e["model"] for e in bucket if e["model"])

        header = f"  Provider: {provider}  ({len(bucket)} firing(s))"
        print(header)
        print("  " + "-" * (len(header) - 2))
        print(f"    Skipped (rule 6 — no new tests needed):  {skipped}")
        print(f"    Errored (key/SDK/network/timeout):       {errored}")
        print(f"    Wrote tests:                             {len(wrote)}")
        print(f"      Files written total:                   {tests_total}")
        print(f"      Combined run PASSED (PASS stands):     {passed}")
        print(f"      Combined run FAILED (retry forced):    {failed}")
        if models:
            for model, n in models.most_common():
                print(f"      Model {model}: {n}")
        if wrote:
            rate = failed / len(wrote)
            print(f"      Catch-or-overspecify rate:             {rate:.0%}")
        print()

    # Cross-provider summary (only useful in multi-provider mode but
    # always shown for consistency).
    all_wrote = [e for e in events if (e["tests_added"] or 0) > 0]
    if all_wrote:
        all_passed = sum(1 for e in all_wrote if e["passed"] is True)
        all_failed = sum(1 for e in all_wrote if e["passed"] is False)
        print("  Across all providers:")
        print(f"    Wrote tests events:    {len(all_wrote)}")
        print(f"    Combined runs passed:  {all_passed}")
        print(f"    Combined runs failed:  {all_failed}")
        print()
        print("  Inspect the failed ones case-by-case to classify "
              "as real catch vs over-specification.")


if __name__ == "__main__":
    main()
