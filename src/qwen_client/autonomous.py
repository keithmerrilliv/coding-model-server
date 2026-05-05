"""qwen-autonomous CLI — submit specs and respond to review gates.

Thin client over the /v1/autonomous/* endpoints. The qwen-server hosts the
SQLite task store and the orchestrator daemon does the actual work; this CLI
just talks HTTP and renders results.

Subcommands:
  submit <spec.md>            Upload a markdown spec, print the new spec_id
  status [<spec_id>]          List recent specs, or details for one
  gates [--spec <spec_id>]    List open review gates
  review <gate_id> ...        Approve or reject a gate
  events <spec_id>            Tail the event log for a spec
  logs <spec_id>              Alias for `events`
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


# ── Server config (mirrors qwen_client/config.py conventions) ────────────────

DEFAULT_HOST = os.getenv("QWEN_SERVER_IP", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("QWEN_SERVER_PORT", "5000"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


def _base_url() -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if ADMIN_API_KEY:
        h["X-Admin-Key"] = ADMIN_API_KEY
    return h


def _die(msg: str, code: int = 1) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _request(method: str, path: str, **kwargs) -> dict | list:
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
    except requests.RequestException as e:
        _die(f"server unreachable at {url}: {e}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        _die(f"{resp.status_code} {detail}")
    return resp.json()


# ── ANSI helpers (subset of qwen_client.config.COLORS) ───────────────────────

class _C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    END = "\033[0m"


def _color(text: str, color: str) -> str:
    return f"{color}{text}{_C.END}" if sys.stdout.isatty() else text


# ── Subcommand handlers ──────────────────────────────────────────────────────

def cmd_submit(args: argparse.Namespace) -> int:
    path = Path(args.spec).expanduser()
    if not path.exists():
        _die(f"spec file not found: {path}")
    markdown = path.read_text()
    if not markdown.strip():
        _die(f"spec file is empty: {path}")

    body = {"markdown": markdown}
    if args.title:
        body["title"] = args.title

    resp = _request("POST", "/v1/autonomous/specs", json=body)
    print(_color(f"✓ spec submitted: {resp['spec_id']}", _C.GREEN))
    print(f"  title:  {resp['title']}")
    print(f"  status: {resp['status']}")
    print()
    print(_color("next steps:", _C.BOLD))
    print(f"  qwen-autonomous status {resp['spec_id']}")
    print(f"  qwen-autonomous gates --spec {resp['spec_id']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.spec_id:
        return _status_one(args.spec_id)
    return _status_list(limit=args.limit)


def _status_list(limit: int = 50) -> int:
    specs = _request("GET", f"/v1/autonomous/specs?limit={limit}")
    if not specs:
        print(_color("(no specs yet)", _C.DIM))
        return 0
    print(_color(f"{'SPEC ID':14}  {'STATUS':22}  {'CREATED':20}  TITLE", _C.BOLD))
    for s in specs:
        status_color = _status_color(s["status"])
        created = s["created_at"][:19].replace("T", " ")
        print(f"{s['id']:14}  "
              f"{_color(s['status'].ljust(22), status_color)}  "
              f"{created:20}  "
              f"{s['title']}")
    return 0


def _status_one(spec_id: str) -> int:
    summary = _request("GET", f"/v1/autonomous/specs/{spec_id}")
    spec = summary["spec"]
    print(_color(f"# {spec['title']}", _C.BOLD))
    print(f"spec_id:    {spec['id']}")
    print(f"status:     {_color(spec['status'], _status_color(spec['status']))}")
    print(f"created:    {spec['created_at']}")
    print(f"updated:    {spec['updated_at']}")
    print(f"tasks:      {summary['task_count']}")
    if spec.get("normalized_yaml"):
        print()
        print(_color("plan.yaml:", _C.CYAN))
        for line in spec["normalized_yaml"].splitlines():
            print(f"  {line}")
    open_gates = summary["open_gates"]
    if open_gates:
        print()
        print(_color(f"open gates ({len(open_gates)}):", _C.YELLOW))
        for g in open_gates:
            print(f"  {g['id']}  {g['gate_type']}")
            print(f"    qwen-autonomous review {g['id']} --approve")
    events = summary["recent_events"]
    if events:
        print()
        print(_color("recent events:", _C.DIM))
        for e in events[:10]:
            ts = e["created_at"][11:19]
            print(f"  {ts}  {e['kind']}")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    qs = f"?spec_id={args.spec}" if args.spec else ""
    gates = _request("GET", f"/v1/autonomous/gates{qs}")
    if not gates:
        print(_color("(no open gates)", _C.DIM))
        return 0
    for g in gates:
        print(_color(f"━━ gate {g['id']} ━━", _C.BOLD))
        print(f"spec:       {g['spec_id']}")
        print(f"type:       {g['gate_type']}")
        print(f"created:    {g['created_at']}")
        print()
        print(g["prompt_md"])
        print()
        print(_color("respond with:", _C.CYAN))
        print(f"  qwen-autonomous review {g['id']} --approve")
        print(f"  qwen-autonomous review {g['id']} --reject --notes 'why'")
        print()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    if args.approve and args.reject:
        _die("--approve and --reject are mutually exclusive")
    if not (args.approve or args.reject):
        _die("must specify --approve or --reject")
    decision = "approved" if args.approve else "rejected"
    body = {"decision": decision}
    if args.notes:
        body["notes"] = args.notes
    resp = _request("POST", f"/v1/autonomous/gates/{args.gate_id}/respond", json=body)
    color = _C.GREEN if decision == "approved" else _C.YELLOW
    print(_color(f"✓ gate {resp['id']} {decision}", color))
    if args.notes:
        print(f"  notes: {args.notes}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    events = _request("GET",
                      f"/v1/autonomous/specs/{args.spec_id}/events?limit={args.limit}")
    if not events:
        print(_color("(no events)", _C.DIM))
        return 0
    for e in events:
        ts = e["created_at"][:19].replace("T", " ")
        kind = e["kind"]
        bits = []
        if e.get("task_id"):
            bits.append(f"task={e['task_id']}")
        if e.get("gate_id"):
            bits.append(f"gate={e['gate_id']}")
        suffix = f"  ({' '.join(bits)})" if bits else ""
        payload_str = ""
        if e.get("payload_json"):
            try:
                payload = json.loads(e["payload_json"])
                payload_str = "  " + _C.DIM + json.dumps(payload) + _C.END
            except json.JSONDecodeError:
                pass
        print(f"{ts}  {kind}{suffix}{payload_str}")
    return 0


def _status_color(status: str) -> str:
    return {
        "pending_plan": _C.DIM,
        "needs_clarification": _C.YELLOW,
        "plan_review": _C.YELLOW,
        "executing": _C.CYAN,
        "done": _C.GREEN,
        "failed": _C.RED,
        "cancelled": _C.DIM,
    }.get(status, _C.END)


# ── Argument parser ──────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qwen-autonomous",
        description="Submit specs and manage autonomous-mode workflows.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit", help="upload a markdown spec")
    p.add_argument("spec", help="path to a .md file")
    p.add_argument("--title", help="override the title (defaults to first H1)")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="show recent specs or details for one")
    p.add_argument("spec_id", nargs="?", help="optional spec id to show in detail")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("gates", help="list open review gates")
    p.add_argument("--spec", help="filter to one spec id")
    p.set_defaults(func=cmd_gates)

    p = sub.add_parser("review", help="approve or reject a gate")
    p.add_argument("gate_id")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--notes", help="markdown notes to attach")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("events", help="tail the event log for a spec")
    p.add_argument("spec_id")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_events)

    # `logs` is a friendly alias for `events`
    p = sub.add_parser("logs", help="alias for `events`")
    p.add_argument("spec_id")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_events)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
