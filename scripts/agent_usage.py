#!/usr/bin/env python3
"""Which agents on the roster are actually used — the evidence for retiring one.

Three sources, because no one of them sees everything:

* requests — every chat completion the server answered logs
  ``chat_completions agent=<name>`` with the name the caller asked for,
  before alias resolution. Pipeline and interactive use both land here. The
  window is whatever the systemd journal still holds.
* pipeline — AGENT_RAN / PLANNER_RAN events in the task store, which reach
  back to the first run but see only autonomous dispatches.
* wiring — what the daemon would dispatch to today, with the repo's .env
  applied: role agents, the implementer rotation, the tier map, the planner,
  synthesis, and the design-review agent only when its stage is switched
  on. A wired agent is kept even when idle, because
  retiring it would break the next dispatch.

A smoke sweep (one request to each of many agents within minutes, as after a
llama-server upgrade) is not use, and its requests are not counted.

An agent is RETIRE when nothing wires it and nothing has requested it for
--days. Seven by default: an eval here runs for one to three consecutive
days, so a silent week means the experiment is over. An alias is RETIRE when
nothing has requested it by that name for --days, or its target is retired.
Only names are printed from .env, never values that are not agent names.

    venv/bin/python scripts/agent_usage.py [--days 7]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_model_server.config import Config  # noqa: E402

_REQUEST_RE = re.compile(r"^(\S+) .*chat_completions agent=(\S+)")
SWEEP_WINDOW = timedelta(minutes=10)
SWEEP_MIN_AGENTS = 6


def _drop_sweeps(requests: list) -> list:
    """Requests minus smoke sweeps: runs in which at least SWEEP_MIN_AGENTS
    distinct names were each requested exactly once within SWEEP_WINDOW."""
    keep = [True] * len(requests)
    i = 0
    while i < len(requests):
        j = i
        while j + 1 < len(requests) and requests[j + 1][0] - requests[i][0] <= SWEEP_WINDOW:
            j += 1
        names = [n for _, n in requests[i:j + 1]]
        if len(set(names)) >= SWEEP_MIN_AGENTS and len(set(names)) == len(names):
            for k in range(i, j + 1):
                keep[k] = False
            i = j + 1
        else:
            i += 1
    return [r for r, k in zip(requests, keep) if k]


def journal_requests() -> "tuple[dict, dict, str | None]":
    """{requested name: count}, {requested name: last ISO time}, earliest time."""
    out = subprocess.run(
        ["journalctl", "-u", "coding-model-server", "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True).stdout
    requests = []
    first = None
    for line in out.splitlines():
        first = first or line.split(" ", 1)[0]
        m = _REQUEST_RE.match(line)
        if m:
            requests.append((datetime.fromisoformat(m.group(1)), m.group(2)))
    counts: dict = defaultdict(int)
    last: dict = {}
    for ts, name in _drop_sweeps(requests):
        counts[name] += 1
        last[name] = ts.isoformat()
    return counts, last, first


def pipeline_events(db_path: Path) -> "tuple[dict, dict]":
    counts: dict = defaultdict(int)
    last: dict = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    for payload, created in con.execute(
            "SELECT payload_json, created_at FROM events "
            "WHERE kind IN ('agent_ran', 'planner_ran') ORDER BY id"):
        try:
            agent = json.loads(payload or "{}").get("agent")
        except ValueError:
            continue
        if agent:
            agent = Config.resolve_agent(agent)
            counts[agent] += 1
            last[agent] = created
    return counts, last


def wiring() -> "dict[str, list[str]]":
    """agent -> what dispatches to it today (code defaults, then .env)."""
    from dotenv import dotenv_values, load_dotenv
    load_dotenv(ROOT / ".env")          # as the daemon starts: .env over code defaults
    from coding_model_autonomous import planner, retry_policy, settings
    wired: dict = defaultdict(list)
    for role, agent in settings.ROLE_TO_AGENT.items():
        wired[Config.resolve_agent(agent)].append(f"role:{role}")
    for a in retry_policy._IMPLEMENTER_ROTATION:
        wired[a].append("rotation")
    for tier, a in retry_policy.TIER_TO_IMPLEMENTER.items():
        wired[a].append(f"tier:{tier}")
    for a in retry_policy.ALLOWED_IMPLEMENTER_AGENTS:
        wired[a].append("recommendable")
    wired[Config.resolve_agent(planner.PLANNER_AGENT)].append("planner")
    if settings.DESIGN_REVIEW_ENABLED:
        wired[Config.resolve_agent(settings.DESIGN_REVIEW_AGENT)].append("design_review")
    wired[Config.resolve_agent(
        os.getenv("AUTONOMOUS_SYNTHESIS_AGENT", "deep_reviewer"))].append("synthesis")
    names = set(Config.AGENTS) | set(Config.AGENT_ALIASES)
    for key, value in dotenv_values(ROOT / ".env").items():
        if value in names:
            wired[Config.resolve_agent(value)].append(f".env:{key}")
    return {a: sorted(set(w)) for a, w in wired.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", type=Path, default=ROOT / "var" / "tasks_db" / "tasks.sqlite")
    args = ap.parse_args()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    req, req_last, first = journal_requests()
    ev, ev_last = pipeline_events(args.db)
    wired = wiring()

    def recent(ts: "str | None") -> bool:
        if not ts:
            return False
        return datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat() >= cutoff

    by_canonical_req: dict = defaultdict(int)
    by_canonical_last: dict = {}
    for name, n in req.items():
        c = Config.resolve_agent(name)
        by_canonical_req[c] += n
        if req_last[name] > by_canonical_last.get(c, ""):
            by_canonical_last[c] = req_last[name]

    print(f"requests: journal since {first}; recent = last {args.days} days\n")
    print(f"{'agent':26} {'requests':>8} {'last request':>12} {'pipeline':>8} "
          f"{'last event':>10}  verdict  wired by")
    retired = set()
    for agent in Config.AGENTS:
        last_req = by_canonical_last.get(agent)
        keep = bool(wired.get(agent)) or recent(last_req)
        if not keep:
            retired.add(agent)
        print(f"{agent:26} {by_canonical_req.get(agent, 0):>8} "
              f"{(last_req or '-')[:10]:>12} {ev.get(agent, 0):>8} "
              f"{(ev_last.get(agent) or '-')[:10]:>10}  "
              f"{'keep  ' if keep else 'RETIRE'}   {', '.join(wired.get(agent, [])) or '-'}")

    print(f"\n{'alias':18} {'-> target':26} {'requests':>8} {'last':>12}  verdict")
    for alias, target in Config.AGENT_ALIASES.items():
        keep = target not in retired and recent(req_last.get(alias))
        print(f"{alias:18} {target:26} {req.get(alias, 0):>8} "
              f"{(req_last.get(alias) or '-')[:10]:>12}  {'keep' if keep else 'RETIRE'}")
    unknown = sorted(n for n in req if n not in Config.AGENTS and n not in Config.AGENT_ALIASES)
    if unknown:
        print(f"\nrequested but not on the roster: {', '.join(unknown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
