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
    python3 eval_agents.py -a dense_architect -a qwen38_architect --tool-loop 3   # DEV-618
"""
import argparse
import collections
import json
import os
import re
import sys
import tempfile
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


# ── tool loop (DEV-618) ───────────────────────────────────────────────────────
# The harness is single-turn: one user message, one completion, judged as is.
# An agentic model under the architect prompt may elect to inspect the
# workspace first — Qwen3.8-27B emitted <<<LIST_DIR>>>/<<<GLOB>>>/<<<PLAN>>>
# and then stopped, awaiting tool results that never came (DEV-616). That is
# an automatic loss for a completion style, not for engineering merit. With
# --tool-loop N the harness answers those markers from an EMPTY sandbox and
# feeds the results back, up to N rounds, before judging the final answer.
# Default 0 — single-turn stays the baseline every prior eval was scored on.
#
# The sandbox is deliberately minimal and read-only: the tasks carry all their
# material in the prompt, so an inspecting model finds nothing and must answer
# from what it was given — the same footing as a model that never inspects.
_READ_TOOLS = ("READ_FILE", "LIST_DIR", "GLOB", "GREP")
_REFUSED_TOOLS = ("REMOTE_EXEC", "WRITE_FILE", "EDIT_FILE", "SAVE_MEMORY", "WEB_SEARCH",
                  "APPLE_DEEP_DOCS", "INGEST_PDF", "DEEP_INGEST")
_ACK_TOOLS = ("PLAN", "SCRATCHPAD", "CONFIDENCE")
_ALL_TAGS = _READ_TOOLS + _REFUSED_TOOLS + _ACK_TOOLS
# A marker's argument is one line for the path/pattern tools and runs to the
# next marker (or the end) for the block tools — the same split the server's
# dispatcher makes, so that a CONFIDENCE line mid-answer does not swallow the
# answer that follows it when the markers are stripped.
_BLOCK_TAGS = ("PLAN", "SCRATCHPAD", "WRITE_FILE", "EDIT_FILE")
_LINE_TAGS = tuple(t for t in _ALL_TAGS if t not in _BLOCK_TAGS)
_ANY_TAG = "|".join(_ALL_TAGS)
_LINE_RE = re.compile(rf"(<{{1,3}})({'|'.join(_LINE_TAGS)})(>{{1,3}})[ \t]*([^\n]*)",
                      re.IGNORECASE)
_BLOCK_RE = re.compile(rf"(<{{1,3}})({'|'.join(_BLOCK_TAGS)})(>{{1,3}})\s*(.*?)(?=<{{1,3}}(?:{_ANY_TAG})>{{1,3}}|\Z)",
                       re.DOTALL | re.IGNORECASE)
_VALID_BRACKETS = frozenset({("<<<", ">>>"), ("<", ">>>"), ("<", ">")})


def _marker_spans(text):
    """[(start, end, TAG, arg)] for every well-formed marker, in order."""
    text = re.sub(r"</?tool_call\s*>", "", text or "")
    spans = []
    for rx in (_LINE_RE, _BLOCK_RE):
        for m in rx.finditer(text):
            if (m.group(1), m.group(3)) not in _VALID_BRACKETS:
                continue
            spans.append((m.start(), m.end(), m.group(2).upper(), m.group(4).strip()))
    spans.sort()
    return text, spans


def parse_markers(text):
    """[(TAG, arg)] for every well-formed tool marker in `text`, in order."""
    return [(tag, arg) for _, _, tag, arg in _marker_spans(text)[1]]


def strip_markers(text):
    """The visible answer with every tool marker (and its argument) removed."""
    text, spans = _marker_spans(text)
    out, pos = [], 0
    for start, end, _, _ in spans:
        if start >= pos:
            out.append(text[pos:start])
            pos = end
    out.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


class EvalSandbox:
    """An empty scratch directory the read-only tools run against."""

    def __init__(self, root=None):
        self.root = os.path.realpath(root or tempfile.mkdtemp(prefix="eval_sandbox_"))

    def _inside(self, path):
        # An absolute path never joins under the root, so it is refused below.
        p = os.path.realpath(os.path.join(self.root, (path or ".").strip()))
        return p if p == self.root or p.startswith(self.root + os.sep) else None

    def run(self, tag, arg):
        if tag in _ACK_TOOLS:
            return "(noted)"
        if tag in _REFUSED_TOOLS:
            return "(not available: the eval sandbox is read-only and offline; answer from the task text)"
        if tag == "LIST_DIR":
            p = self._inside(arg)
            if p is None:
                return "(refused: path is outside the sandbox)"
            if not os.path.isdir(p):
                return f"(no such directory: {arg or '.'})"
            names = sorted(os.listdir(p))
            return "\n".join(names) if names else "(empty directory)"
        if tag == "READ_FILE":
            p = self._inside(arg)
            if p is None:
                return "(refused: path is outside the sandbox)"
            if not os.path.isfile(p):
                return f"(no such file: {arg})"
            with open(p, errors="replace") as fh:
                return fh.read(20000)
        if tag == "GLOB":
            import glob as _glob
            hits = sorted(os.path.relpath(h, self.root) for h in
                          _glob.glob(os.path.join(self.root, arg or "*"), recursive=True))
            return "\n".join(hits) if hits else "(no matches)"
        if tag == "GREP":
            pattern = (arg.split("|", 1)[0] if arg else "").strip()
            if not pattern:
                return "(grep: empty pattern)"
            hits = []
            for dirpath, _, files in os.walk(self.root):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    try:
                        with open(fp, errors="replace") as fh:
                            for n, line in enumerate(fh, 1):
                                if re.search(pattern, line):
                                    hits.append(f"{os.path.relpath(fp, self.root)}:{n}: {line.rstrip()}")
                    except OSError:
                        continue
            return "\n".join(hits[:200]) if hits else "(no matches)"
        return "(unknown tool)"


# DEV-723. A single 5xx used to abort the whole run. The harness batches by
# agent -- every task for A, then every task for B, then judging -- so a 502 on
# the last generation threw away ~40 minutes of finished work.
#
# The 502 that prompted this is the server's "model produced no visible content
# (reasoning-only response)": a thinking-on model spent its whole budget
# reasoning, the server stripped the reasoning and found an empty string. That
# is STOCHASTIC, not deterministic -- the same prompt at temperature 0.0
# returned 1400 tokens once and 305 the next time under MTP speculative decode
# -- so a retry genuinely recovers it. Raising the budget (below) is the real
# fix; this is the seatbelt.
#
# 4xx is NOT retried: a bad agent name or a bad key should fail immediately
# rather than be retried into a timeout.
# DEV-723. The budget was 1400, BELOW what a thinking-on architect needs.
# Since DEV-556 the reasoning and the answer SHARE this budget, so 1400 either
# truncated the answer (finish_reason=length) or was consumed entirely by
# reasoning, which the server rejects as a reasoning-only 502. DEV-702's own
# baseline logs show single completions of 2,801-6,083 tokens against it.
#
# Anchored to production rather than picked: an eval that measures a model
# under a budget production never imposes is measuring the harness. A cap is a
# ceiling, not a target -- measured, the same task returned 589 chars and
# stopped under a 4000 cap, so models do not inflate to fill it. Imported so
# that if production's budget moves and this does not, the test says so.
try:
    from coding_model_autonomous.executor import ARCHITECT_MAX_TOKENS as _PROD_BUDGET
except Exception:                                    # pragma: no cover
    _PROD_BUDGET = 8000
DEFAULT_MAX_TOKENS = _PROD_BUDGET

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
COMPLETION_ATTEMPTS = 4


def _completion(server, headers, agent, messages, max_tokens):
    for attempt in range(1, COMPLETION_ATTEMPTS + 1):
        last = attempt == COMPLETION_ATTEMPTS
        try:
            r = requests.post(
                f"{server}/v1/chat/completions", headers=headers, timeout=1800,
                json={"model": agent, "messages": messages,
                      "max_tokens": max_tokens, "temperature": 0.0,
                      "stream": False})
        except requests.exceptions.RequestException as exc:
            if last:
                raise
            print(f"    [retry {attempt}/{COMPLETION_ATTEMPTS - 1}] {agent}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            time.sleep(5 * attempt)
            continue

        if r.status_code in _RETRYABLE_STATUS and not last:
            detail = (r.text or "")[:160].replace("\n", " ")
            print(f"    [retry {attempt}/{COMPLETION_ATTEMPTS - 1}] {agent}: "
                  f"HTTP {r.status_code} {detail}", flush=True)
            time.sleep(5 * attempt)
            continue

        r.raise_for_status()   # 4xx, or a 5xx on the final attempt
        body = r.json()
        choice = (body.get("choices") or [{}])[0]
        return (choice.get("message", {}).get("content", "") or "",
                (body.get("usage") or {}).get("completion_tokens", 0),
                choice.get("finish_reason"))
    raise RuntimeError("unreachable")   # pragma: no cover


def ask_agent(server, headers, agent, prompt, max_tokens, system=None, tool_loop=0, sandbox=None):
    """One answer through the real server path (system prompt, budget, RAG).

    With tool_loop == 0 this is exactly one completion, judged as is. With
    tool_loop == N, a completion that carries tool markers gets them answered
    from the sandbox and is asked to continue, up to N rounds; the judged text
    is the final completion with its markers stripped. Every answer records
    how many rounds it used and whether the budget ran out mid-inspection.
    """
    t0 = time.time()
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    tokens, rounds, calls = 0, 0, []
    text, n, finish = _completion(server, headers, agent, messages, max_tokens)
    tokens += n
    finishes = [finish]
    exhausted = False
    while tool_loop:
        markers = parse_markers(text)
        if not markers:
            break
        if rounds >= tool_loop:
            exhausted = True
            break
        rounds += 1
        sandbox = sandbox or EvalSandbox()
        lines = []
        for tag, arg in markers:
            calls.append(f"{tag} {arg}".strip())
            lines.append(f"<<<{tag}>>>{arg}\n{sandbox.run(tag, arg)}")
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content":
                         f"# TOOL RESULTS (round {rounds}/{tool_loop})\n\n" + "\n\n".join(lines)
                         + "\n\nContinue. When you have what you need, write the complete final "
                           "answer with no tool markers."})
        text, n, finish = _completion(server, headers, agent, messages, max_tokens)
        finishes.append(finish)
        tokens += n
    return {
        "text": strip_markers(text) if tool_loop else text,
        "completion_tokens": tokens,
        "wall": time.time() - t0,
        "tool_rounds": rounds,
        "tool_calls": calls,
        "tool_exhausted": exhausted,
        # DEV-723 follow-up: the DEFINITIVE truncation signal. Inferring it from
        # completion_tokens == max_tokens is a guess that breaks as soon as a
        # tool loop sums several calls. "length" on ANY call means this answer
        # was cut off and the judge is scoring a truncated design.
        "finish_reasons": finishes,
        "truncated": "length" in finishes,
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
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                    help=f"output budget per completion (default "
                         f"{DEFAULT_MAX_TOKENS}, matching production's "
                         f"architect budget; see DEV-723)")
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
    ap.add_argument("--tool-loop", type=int, default=0, metavar="N",
                    help="answer tool markers (<<<LIST_DIR>>> etc.) from an empty "
                         "read-only sandbox and let the model continue, up to N "
                         "rounds, before judging (DEV-618). Default 0: single-turn, "
                         "the baseline every prior eval was scored on.")
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
    mode = f" | tool-loop={args.tool_loop}" if args.tool_loop else ""
    print(f"{len(tasks)} tasks | {x} vs {y} | judge={args.judge}{mode}\n")

    # Phase 1 -- one model load per agent, not one per task. Answers are
    # deterministic (temp 0), so --reuse-answers skips regeneration entirely when
    # only the judging failed (e.g. the judge rate-limited mid-run).
    if args.reuse_answers:
        saved = json.load(open(args.reuse_answers))
        answers = saved["answers"]
        missing = [a for a in (x, y) if a not in answers]
        if missing:
            ap.error(f"--reuse-answers {args.reuse_answers} has no answers for {missing}")
        excluded = collections.defaultdict(list)
        print(f"### reusing saved answers for {x}, {y} (skipping generation)\n", flush=True)
    else:
        answers = {}
        excluded = collections.defaultdict(list)   # task id -> why, per agent
        sandbox = EvalSandbox() if args.tool_loop else None
        for agent in (x, y):
            print(f"### {agent} answering (first call pays the model load)", flush=True)
            answers[agent] = {}
            for t in tasks:
                # DEV-723: one unanswerable task must not discard the other
                # five. Record the failure, carry on, and exclude the task
                # from judging rather than scoring a missing answer as a loss
                # -- a transport failure is not a quality signal.
                try:
                    a = ask_agent(args.server, headers, agent, t["prompt"],
                                  args.max_tokens, system=args.system,
                                  tool_loop=args.tool_loop, sandbox=sandbox)
                except Exception as exc:
                    excluded[t["id"]].append(f"{agent}: {type(exc).__name__}: {exc}")
                    print(f"  {t['id']:16} FAILED after {COMPLETION_ATTEMPTS} "
                          f"attempts -- {type(exc).__name__} -- task excluded",
                          flush=True)
                    continue
                answers[agent][t["id"]] = a
                tools = ""
                if args.tool_loop:
                    tools = (f"  {a['tool_rounds']} tool round(s)"
                             + ("  EXHAUSTED" if a["tool_exhausted"] else ""))
                print(f"  {t['id']:16} {a['completion_tokens']:5d} tok  {a['wall']:6.1f}s{tools}", flush=True)
            print()
        # Checkpoint before judging: generation is the expensive, GPU-bound half,
        # and the judge is a flaky external API. Persist now so a judge failure
        # never costs the answers — re-run with --reuse-answers to judge only.
        json.dump({"agents": [x, y], "judge": args.judge, "tool_loop": args.tool_loop,
                   "max_tokens": args.max_tokens,
                   "excluded": {k: v for k, v in excluded.items()},
                   "answers": answers},
                  open(args.out, "w"), indent=1)
        print(f"(answers checkpointed to {args.out})\n", flush=True)

    # DEV-723: a task is judgeable only if BOTH agents answered it.
    judgeable = [t for t in tasks
                 if t["id"] in answers[x] and t["id"] in answers[y]]
    dropped = [t["id"] for t in tasks if t not in judgeable]
    if dropped:
        print(f"### EXCLUDED {len(dropped)} of {len(tasks)} task(s) -- no answer "
              f"from one or both agents:", flush=True)
        for tid in dropped:
            for why in excluded.get(tid, ["(no answer recorded)"]):
                print(f"      {tid}: {why}", flush=True)
        print(flush=True)

    # Phase 2 -- judge each task in both orders; a flip means position bias, not merit.
    print("### judging (each task twice, order swapped)\n", flush=True)
    results, transcripts = [], {}
    for i, t in enumerate(judgeable):
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
    print(f"{x}: {tally[x]}   {y}: {tally[y]}   tie: {tally['tie']}   "
          f"(of {len(judgeable)} judged"
          + (f", {len(dropped)} EXCLUDED" if dropped else "") + ")")
    print("=" * 62)
    flips = [r["id"] for r in results if not r["consistent"]]
    if flips:
        print(f"order-dependent (judge position bias, scored tie): {', '.join(flips)}")

    for agent in (x, y):
        tot = sum(a["wall"] for a in answers[agent].values())
        tok = sum(a["completion_tokens"] for a in answers[agent].values())
        print(f"{agent:14} {tok:6d} tok in {tot:6.1f}s")

    json.dump({"agents": [x, y], "judge": args.judge, "tool_loop": args.tool_loop,
               # DEV-723: record the budget. Neither DEV-702 artifact did, so
               # reconstructing that run's budget meant reading token counts
               # out of a log.
               "max_tokens": args.max_tokens,
               "excluded": {k: v for k, v in excluded.items()},
               "results": results,
               "answers": answers, "transcripts": transcripts},
              open(args.out, "w"), indent=1)
    print(f"\nfull answers + judge reasoning: {args.out}")


if __name__ == "__main__":
    main()
