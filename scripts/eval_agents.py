#!/usr/bin/env python3
"""Blind, counterbalanced head-to-head between two agents, scored by an external judge.

Answers the question a decode benchmark cannot: which model is actually BETTER.

Design decisions that matter, and why:

  External judge. The obvious judge is `deep_reviewer`, but it is Qwen-family and
  so is `implementer` — a local judge scoring its own lineage is not evidence.
  Default is Claude via external_judges. `--judge claude-sdk` runs Claude through
  the Claude Code subscription (no API credit needed — the reliable path when the
  API key is empty and Gemini's free tier is rate-capped; see DEV-98). Pass
  --judge deep_reviewer to use the local one anyway, with that caveat in mind.

  Counterbalanced. LLM judges have a well-documented position bias toward the
  first response shown. Every task is therefore judged TWICE, with the order
  swapped. A model only WINS a task if it wins under BOTH orderings; if the
  verdict flips when the order flips, the judge was reading position, not
  quality, and the task is scored a tie. This throws away real signal on close
  calls -- deliberately. A win here should mean something.

  Blind. Responses are labelled A/B only, and obvious self-identification
  ("I am Qwen") is scrubbed before judging.

  Batched by agent. Each agent answers every task before we swap models: two
  model loads total, not two per task.

Usage:
    ADMIN_API_KEY=... python3 eval_agents.py -a implementer -a ornith
    python3 eval_agents.py -a implementer -a ornith --judge deep_reviewer
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from coding_model_server.external_judges import (  # noqa: E402
    call_claude, call_claude_sdk, call_gemini)

HERE = os.path.dirname(os.path.abspath(__file__))

JUDGE_SYSTEM = """You are grading two AI models on a software engineering task.

Judge ONLY on engineering merit, in this order of importance:
  1. Correctness -- does the diagnosis/code actually hold up? Wrong-but-confident is the worst outcome.
  2. Completeness -- are the edge cases the task calls out actually handled?
  3. Insight -- does it explain the underlying mechanism, or just pattern-match?
  4. Concision -- padding, restating the question, and hedging are all faults.

Ignore formatting polish, response length, and tone. Longer is not better.
If one response is confidently wrong on the core question, it loses outright,
regardless of how thorough it looks.

Think it through, then end your reply with exactly one line:
VERDICT: A    (A is better)
VERDICT: B    (B is better)
VERDICT: TIE  (genuinely indistinguishable on merit)"""

# Scrubbed before judging so the judge cannot infer which family wrote what.
_IDENTITY = re.compile(
    r"\b(I am|I'm|As)\s+(Qwen|Ornith|Claude|GPT|ChatGPT|Llama|DeepSeek)[\w.\-]*\b",
    re.IGNORECASE)


def scrub(text):
    return _IDENTITY.sub("I am an AI assistant", text)


def ask_agent(server, headers, agent, prompt, max_tokens, system=None):
    """One completion through the real server path (system prompt, budget, RAG)."""
    t0 = time.time()
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    r = requests.post(
        f"{server}/v1/chat/completions", headers=headers, timeout=1800,
        json={"model": agent, "messages": messages,
              "max_tokens": max_tokens, "temperature": 0.0, "stream": False})
    r.raise_for_status()
    body = r.json()
    return {
        "text": (body.get("choices") or [{}])[0].get("message", {}).get("content", ""),
        "completion_tokens": (body.get("usage") or {}).get("completion_tokens", 0),
        "wall": time.time() - t0,
    }


def _with_retry(fn, attempts=6):
    """External judges rate-limit and 503 under load. A bare judge call has no
    retry, so one blip anywhere in 2*len(tasks) calls aborts the whole run —
    losing every generated answer, which is the expensive part. Retry with
    backoff on ANY exception; re-raise only if every attempt fails.

    Backoff crosses the free-tier RPM reset window. Gemini's 429 says "retry in
    ~34s" (a per-minute quota of 20 requests), so late attempts must wait longer
    than that: 8, 16, 24, 32, 40s. Pacing in the caller should keep us under the
    limit in the first place; this is the recovery if a burst slips through."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — transient API errors are opaque; retry all
            if i == attempts - 1:
                raise
            delay = 8 * (i + 1)
            print(f"    judge attempt {i+1}/{attempts} failed ({type(e).__name__}); "
                  f"retrying in {delay}s", flush=True)
            time.sleep(delay)


def judge(judge_name, task, first, second, server, headers):
    """Return 'A' | 'B' | 'TIE'. A is always `first` as shown to the judge."""
    content = (f"# TASK\n{task['prompt']}\n\n"
               f"# RESPONSE A\n{scrub(first)}\n\n"
               f"# RESPONSE B\n{scrub(second)}\n")
    if judge_name == "claude":
        out = _with_retry(lambda: call_claude(JUDGE_SYSTEM, content, max_tokens=2000, timeout=300))
    elif judge_name == "claude-sdk":
        out = _with_retry(lambda: call_claude_sdk(JUDGE_SYSTEM, content, max_tokens=2000, timeout=300))
    elif judge_name == "gemini":
        out = _with_retry(lambda: call_gemini(JUDGE_SYSTEM, content, max_tokens=2000, timeout=300))
    else:  # a local agent, e.g. deep_reviewer
        r = requests.post(
            f"{server}/v1/chat/completions", headers=headers, timeout=1800,
            json={"model": judge_name,
                  "messages": [{"role": "user", "content": JUDGE_SYSTEM + "\n\n" + content}],
                  "max_tokens": 2000, "temperature": 0.0, "stream": False})
        r.raise_for_status()
        out = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")

    m = re.findall(r"VERDICT:\s*(A|B|TIE)", out, re.IGNORECASE)
    return (m[-1].upper() if m else "TIE"), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--agent", action="append", dest="agents", required=True,
                    help="exactly two, e.g. -a implementer -a ornith")
    ap.add_argument("--tasks", default=os.path.join(HERE, "eval_tasks.json"))
    ap.add_argument("--judge", default="claude",
                    help="claude (API key) | claude-sdk (Claude Code subscription, "
                         "no API credit needed) | gemini | any local agent name "
                         "(e.g. deep_reviewer)")
    ap.add_argument("--max-tokens", type=int, default=1400)
    ap.add_argument("--system", metavar="TEXT_OR_@FILE", default=None,
                    help="system prompt sent with every generation request, "
                         "@path reads it from a file. The server honours a "
                         "client-supplied system message and skips the agent's "
                         "default — needed when the default (interactive, "
                         "tool-teaching) prompt would contaminate the "
                         "measurement (DEV-562).")
    ap.add_argument("--server", default="http://127.0.0.1:5000")
    ap.add_argument("--out", default="/tmp/eval_agents.json")
    ap.add_argument("--reuse-answers", metavar="JSON",
                    help="skip generation; load the `answers` block from a prior "
                         "--out file. Answers are deterministic (temp 0), so a "
                         "re-judge never needs to re-run the models.")
    ap.add_argument("--judge-interval", type=float, default=4.0,
                    help="min seconds between judge calls. The Gemini free tier "
                         "caps at 20 requests/min; 4s (=15/min) stays under it. "
                         "Set 0 for a paid judge with no RPM limit.")
    args = ap.parse_args()

    if len(args.agents) != 2:
        ap.error("need exactly two -a/--agent")
    x, y = args.agents

    if args.system and args.system.startswith("@"):
        args.system = open(args.system[1:]).read()

    headers = {"Content-Type": "application/json"}
    if (k := os.getenv("ADMIN_API_KEY")):
        headers["Authorization"] = f"Bearer {k}"

    tasks = json.load(open(args.tasks))
    print(f"{len(tasks)} tasks | {x} vs {y} | judge={args.judge}\n")

    # Phase 1 -- one model load per agent, not one per task. Answers are
    # deterministic (temp 0), so --reuse-answers skips regeneration entirely when
    # only the judging failed (e.g. the judge rate-limited mid-run).
    if args.reuse_answers:
        saved = json.load(open(args.reuse_answers))
        answers = saved["answers"]
        missing = [a for a in (x, y) if a not in answers]
        if missing:
            ap.error(f"--reuse-answers {args.reuse_answers} has no answers for {missing}")
        print(f"### reusing saved answers for {x}, {y} (skipping generation)\n", flush=True)
    else:
        answers = {}
        for agent in (x, y):
            print(f"### {agent} answering (first call pays the model load)", flush=True)
            answers[agent] = {}
            for t in tasks:
                a = ask_agent(args.server, headers, agent, t["prompt"],
                              args.max_tokens, system=args.system)
                answers[agent][t["id"]] = a
                print(f"  {t['id']:16} {a['completion_tokens']:5d} tok  {a['wall']:6.1f}s", flush=True)
            print()
        # Checkpoint before judging: generation is the expensive, GPU-bound half,
        # and the judge is a flaky external API. Persist now so a judge failure
        # never costs the answers — re-run with --reuse-answers to judge only.
        json.dump({"agents": [x, y], "judge": args.judge, "answers": answers},
                  open(args.out, "w"), indent=1)
        print(f"(answers checkpointed to {args.out})\n", flush=True)

    # Phase 2 -- judge each task in both orders; a flip means position bias, not merit.
    print("### judging (each task twice, order swapped)\n", flush=True)
    results, transcripts = [], {}
    for i, t in enumerate(tasks):
        tx, ty = answers[x][t["id"]]["text"], answers[y][t["id"]]["text"]

        # Pace to stay under the judge's RPM limit (see --judge-interval). Two
        # calls per task, so half the interval between the paired calls too.
        if i and args.judge_interval:
            time.sleep(args.judge_interval)
        v1, o1 = judge(args.judge, t, tx, ty, args.server, headers)   # A=x, B=y
        if args.judge_interval:
            time.sleep(args.judge_interval)
        v2, o2 = judge(args.judge, t, ty, tx, args.server, headers)   # A=y, B=x

        # Translate each verdict into "who won", independent of shown position.
        w1 = {"A": x, "B": y, "TIE": "tie"}[v1]
        w2 = {"A": y, "B": x, "TIE": "tie"}[v2]
        winner = w1 if w1 == w2 else "tie"
        consistent = w1 == w2

        results.append({"id": t["id"], "kind": t["kind"], "winner": winner,
                        "order1": w1, "order2": w2, "consistent": consistent})
        transcripts[t["id"]] = {"order1": o1, "order2": o2}
        flag = "" if consistent else "  (order-dependent -> tie)"
        print(f"  {t['id']:16} {w1:12} / {w2:12} -> {winner}{flag}", flush=True)

    tally = Counter(r["winner"] for r in results)
    print("\n" + "=" * 62)
    print(f"{x}: {tally[x]}   {y}: {tally[y]}   tie: {tally['tie']}   (of {len(tasks)})")
    print("=" * 62)
    flips = [r["id"] for r in results if not r["consistent"]]
    if flips:
        print(f"order-dependent (judge position bias, scored tie): {', '.join(flips)}")

    for agent in (x, y):
        tot = sum(a["wall"] for a in answers[agent].values())
        tok = sum(a["completion_tokens"] for a in answers[agent].values())
        print(f"{agent:14} {tok:6d} tok in {tot:6.1f}s")

    json.dump({"agents": [x, y], "judge": args.judge, "results": results,
               "answers": answers, "transcripts": transcripts},
              open(args.out, "w"), indent=1)
    print(f"\nfull answers + judge reasoning: {args.out}")


if __name__ == "__main__":
    main()
