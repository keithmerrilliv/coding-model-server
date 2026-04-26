#!/usr/bin/env python3
"""Auto-approve every PENDING review gate for a spec, in a polling loop.

Used to drive the autonomous pipeline through to terminal status without
human intervention — the supervisor's transition decisions are still
exercised via test failures, not gate rejections.

Exits when the spec reaches a terminal status (done / failed / cancelled).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

HOST = os.getenv("QWEN_SERVER_IP", "127.0.0.1")
PORT = int(os.getenv("QWEN_SERVER_PORT", "5000"))
KEY = os.getenv("ADMIN_API_KEY", "")

TERMINAL_STATES = {"done", "failed", "cancelled"}


def _base() -> str:
    return f"http://{HOST}:{PORT}"


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if KEY:
        h["X-Admin-Key"] = KEY
    return h


def _get(path: str):
    r = requests.get(f"{_base()}{path}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict):
    r = requests.post(f"{_base()}{path}", json=body, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _spec_status(spec_id: str) -> str:
    data = _get(f"/v1/autonomous/specs/{spec_id}")
    return data["spec"]["status"]


def _open_gates(spec_id: str) -> list[dict]:
    return _get(f"/v1/autonomous/gates?spec_id={spec_id}")


def _approve(gate_id: str, notes: str = "auto-approved by test runner"):
    return _post(f"/v1/autonomous/gates/{gate_id}/respond",
                 {"decision": "approved", "notes": notes})


def main() -> int:
    parser = argparse.ArgumentParser(prog="auto_approve_gates")
    parser.add_argument("spec_id")
    parser.add_argument("--interval", type=float, default=15.0,
                        help="seconds between polls (default 15)")
    parser.add_argument("--max-minutes", type=float, default=90.0,
                        help="hard timeout in minutes (default 90)")
    args = parser.parse_args()

    deadline = time.time() + args.max_minutes * 60
    seen_gates: set[str] = set()
    last_status: str | None = None

    print(f"[auto-approve] watching spec={args.spec_id} "
          f"(interval={args.interval}s, max={args.max_minutes}min)",
          flush=True)

    while time.time() < deadline:
        try:
            status = _spec_status(args.spec_id)
        except requests.RequestException as e:
            print(f"[auto-approve] status fetch failed: {e}", flush=True)
            time.sleep(args.interval)
            continue

        if status != last_status:
            print(f"[auto-approve] status: {status}", flush=True)
            last_status = status

        if status in TERMINAL_STATES:
            print(f"[auto-approve] terminal status reached: {status}",
                  flush=True)
            return 0 if status == "done" else 1

        try:
            gates = _open_gates(args.spec_id)
        except requests.RequestException as e:
            print(f"[auto-approve] gates fetch failed: {e}", flush=True)
            time.sleep(args.interval)
            continue

        for g in gates:
            gid = g["id"]
            if gid in seen_gates:
                continue
            seen_gates.add(gid)
            try:
                _approve(gid)
                print(f"[auto-approve] APPROVED gate {gid} ({g['gate_type']})",
                      flush=True)
            except requests.RequestException as e:
                print(f"[auto-approve] approve {gid} failed: {e}", flush=True)
                seen_gates.discard(gid)

        time.sleep(args.interval)

    print(f"[auto-approve] HARD TIMEOUT after {args.max_minutes}min "
          f"(last status={last_status})", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
