#!/usr/bin/env python3
"""DEV-901: replay archived syntheses through any agent, offline, and score them.

    venv/bin/python scripts/replay_synthesis.py <agent> [--specs ID ...]
        [--population done|all] [--prompt-only] [--out DIR]

For each spec that reached synthesis, the archived workspace and the task
database are copied into a scratch directory as they stood when synthesis
STARTED, and the daemon's own ``_run_synthesis`` runs against the copies. Only
three things are swapped:

* the model call goes to *agent* (the prompt is budgeted against the
  ``synthesizer`` window whatever the arm, so every arm is sent the same bytes;
  an arm too small for them sheds attempts on its own 413, and the hash shows
  it);
* the repository read is served from the archived ``context.json`` instead of
  the live repo, so the protected scaffold is the one the run was shown;
* the tests run at the commit the run was planned against, never at current
  main: the Mac runner at the ``local_sha`` the context recorded, the
  self-target overlay at main as it stood when synthesis started.

Everything else, the prompt builder, the allocator, the normaliser, the write
guards, the structural test guard and the one repair round, is the pipeline's
code, so a replay scores the way the pipeline scores.

Read-only against the archive. Nothing is written under var/, no event reaches
the live database, nothing is delivered. A spec whose state cannot be rebuilt
is reported ``unreproducible`` with the reason, never scored as a failure.

Writes one JSONL row per (spec, agent) to --out (default
var/replay_synthesis/<agent>.jsonl, and scratch beside it). Model calls go to
the live server, so run arms only with nothing in flight.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

ARCHIVE_DB = REPO / "var" / "tasks_db" / "tasks.sqlite"
ARCHIVE_SPECS = REPO / "var" / "tasks_db" / "specs"
# The allocation baseline: every arm's prompt is budgeted against this window.
BUDGET_AGENT = "synthesizer"
MAC_FRAMEWORKS = ("swift_test", "xcodebuild_test")

logger = logging.getLogger("replay_synthesis")


def load_env() -> None:
    """The orchestrator's environment: systemd reads the repo .env and then the
    config one, the later winning; load_dotenv keeps the first value it sees,
    so the config file goes first. Must run before any coding_model import,
    because settings read the environment at import time."""
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".config" / "coding-model-server" / ".env")
    load_dotenv(REPO / ".env")


# ── the archive ──────────────────────────────────────────────────────────────

@dataclass
class Synthesis:
    """One archived synthesis call, located in the event log."""
    spec_id: str
    status: str               # the spec's final status
    ran_at: str               # the synthesizer agent_ran event (after the call)
    started_at: str           # ran_at less the call's duration
    attempts_total: int       # attempts the call was built from
    agent: str | None
    archived_verdict: str     # passed | failed | unknown
    payload: dict = field(default_factory=dict)


def _iso_minus_ms(iso: str, ms: int) -> str:
    return (datetime.fromisoformat(iso) - timedelta(milliseconds=ms)).isoformat()


def _archived_verdict(conn: sqlite3.Connection, spec_id: str, ran_at: str) -> str:
    """What the pipeline concluded from this synthesis: a release_approval gate
    means its tests passed (after any repair); a failed task or a failed repair
    test run means they did not. Stops at the next synthesis of the spec."""
    rows = conn.execute(
        "SELECT kind, payload_json FROM events WHERE spec_id=? AND created_at>? "
        "ORDER BY created_at", (spec_id, ran_at)).fetchall()
    for kind, raw in rows:
        payload = json.loads(raw or "{}")
        if (kind == "agent_ran" and payload.get("role") == "synthesizer"
                and payload.get("model_call") is not False):
            break
        if kind == "gate_created" and payload.get("gate_type") == "release_approval":
            return "passed"
        if kind == "task_status_changed" and payload.get("new_status") == "failed":
            return "failed"
        if kind == "spec_status_changed" and payload.get("new_status") == "failed":
            return "failed"
    return "unknown"


def find_syntheses(db_path: Path = ARCHIVE_DB) -> dict[str, list[Synthesis]]:
    """Every archived synthesis call, by spec, oldest first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT e.spec_id, s.status, e.created_at, e.payload_json "
            "FROM events e JOIN specs s ON s.id = e.spec_id "
            "WHERE e.kind='agent_ran' "
            "AND json_extract(e.payload_json, '$.role')='synthesizer' "
            "ORDER BY e.created_at").fetchall()
        out: dict[str, list[Synthesis]] = {}
        for spec_id, status, ran_at, raw in rows:
            p = json.loads(raw or "{}")
            if p.get("model_call") is False:
                # A write-guard or normaliser record filed under the role,
                # not a call.
                continue
            out.setdefault(spec_id, []).append(Synthesis(
                spec_id=spec_id, status=status, ran_at=ran_at,
                started_at=_iso_minus_ms(ran_at, int(p.get("duration_ms") or 0)),
                attempts_total=int(p.get("attempts_total") or p.get("attempts") or 0),
                agent=p.get("agent"),
                archived_verdict=_archived_verdict(conn, spec_id, ran_at),
                payload=p))
        return out
    finally:
        conn.close()


class Unreproducible(Exception):
    """The archive cannot rebuild what synthesis saw; the reason says why."""


def build_scratch_db(spec_id: str, cutoff: str, dest: Path,
                     source: Path = ARCHIVE_DB) -> Path:
    """A copy of the task database holding only *spec_id*, as it stood at
    *cutoff*: the rejection notes, gates and tasks synthesis could have seen.
    The live database is only ever read (the backup API reads through the
    WAL, so the copy is current)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("DELETE FROM specs WHERE id != ?", (spec_id,))
        for table in ("tasks", "artifacts", "review_gates", "events"):
            dst.execute(f"DELETE FROM {table} WHERE spec_id != ?", (spec_id,))
            dst.execute(f"DELETE FROM {table} WHERE created_at >= ?", (cutoff,))
        dst.commit()
        dst.execute("VACUUM")
    finally:
        src.close()
        dst.close()
    return dest


def _numbered_retries(history: Path) -> dict[int, Path]:
    out = {}
    for sub in history.iterdir() if history.is_dir() else ():
        if sub.is_dir() and sub.name.startswith("retry_"):
            try:
                out[int(sub.name.split("_", 1)[1])] = sub
            except ValueError:
                continue
    return out


def build_scratch_workspace(spec_id: str, attempts_total: int, dest: Path,
                            archive: Path = ARCHIVE_SPECS) -> Path:
    """The spec directory as synthesis found it.

    At synthesis time retry_history held attempts 0..N-2 and the live
    directory held attempt N-1; the daemon snapshots the live attempt as
    retry_<N-1> only after the call. So the archive's retry_0..retry_<N-1>
    are exactly the attempts the call saw, the last goes back to the top
    level (it is rendered as ``agent=current``), and anything later, the
    synthesis snapshot or a retry after it, is left out.
    """
    src = archive / spec_id
    if not src.is_dir():
        raise Unreproducible(f"no archived workspace at {src}")
    retries = _numbered_retries(src / "retry_history")
    missing = [i for i in range(attempts_total) if i not in retries]
    if attempts_total < 1 or missing:
        raise Unreproducible(
            f"synthesis saw {attempts_total} attempt(s) but the archive lacks "
            f"retry_{missing[0] if missing else 0}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    # The inputs of earlier phases stay where the daemon keeps them.
    from coding_model_autonomous.retry_policy import _PRESERVE_ON_RETRY
    for name in _PRESERVE_ON_RETRY:
        if (src / name).is_file():
            shutil.copy2(src / name, dest / name)
    last = retries[attempts_total - 1]
    shutil.copytree(last, dest, dirs_exist_ok=True)
    for i in range(attempts_total - 1):
        shutil.copytree(retries[i], dest / "retry_history" / f"retry_{i}")
    # The last attempt's snapshot carries the context as it stood then; a
    # workspace from before snapshots kept it falls back to the archive's.
    if not (dest / "context.json").is_file() and (src / "context.json").is_file():
        shutil.copy2(src / "context.json", dest / "context.json")
    return dest


def archived_fetch(context_json: Path):
    """A repository read served from the archived context: the files the run
    was shown, and for any other path the reason the run recorded (or 'not
    found in the archive'), so a path the current code asks for and the run
    never did reads as missing rather than as live repository content."""
    data = json.loads(context_json.read_text()) if context_json.is_file() else {}
    served = {**(data.get("editable") or {}), **(data.get("protected") or {})}
    omitted = {o["path"]: o.get("reason") or "unreadable"
               for o in data.get("omitted") or []}
    state = data.get("ref_state") or {}

    def fetch(repo, paths, base_ref="HEAD", timeout=30, ref_state=None):
        files = [(p, served[p]) for p in paths if p in served]
        problems = [f"{p}: {omitted.get(p, 'not found in the archive')}"
                    for p in paths if p not in served]
        if ref_state is not None and state:
            ref_state.update(state)
        return files, problems

    return fetch


def pinned_ref(framework: str, repo: str | None, context_json: Path,
               started_at: str) -> str | None:
    """The commit a replay's tests run at, or None when none was recorded or
    none is needed.

    Mac frameworks: the sha the runner reported serving the context from.
    A local framework that overlays this repository: main as it stood when
    synthesis started (``git archive`` of that commit replaces the overlay's
    HEAD). Any other local framework tests the workspace alone."""
    if framework in MAC_FRAMEWORKS:
        data = json.loads(context_json.read_text()) if context_json.is_file() else {}
        return ((data.get("ref_state") or {}).get("local_sha")) or None
    if repo != REPO.name:
        return None
    out = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "-1", f"--before={started_at}", "main"],
        capture_output=True, text=True)
    return out.stdout.strip() or None


# ── one replay ───────────────────────────────────────────────────────────────

def prompt_hash(messages) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


class _PromptOnly(Exception):
    """Raised in place of the model call when only the prompt is wanted."""


def _test_counts(output: str, framework: str, passed: bool) -> dict:
    from coding_model_autonomous import diagnostics
    report = diagnostics.read(output, framework, passed=passed)
    return {"passed": passed, "verdict": report.verdict,
            "passed_n": report.passed, "total": report.total}


def replay(syn: Synthesis, agent: str, scratch: Path, *,
           prompt_only: bool = False, archive_db: Path = ARCHIVE_DB,
           archive_specs: Path = ARCHIVE_SPECS) -> dict:
    """Replay *syn* through *agent*; return the JSONL row."""
    import coding_model_server.orchestrator_daemon as d
    from coding_model_autonomous import test_runner
    from coding_model_autonomous.db import Database

    row: dict = {"spec": syn.spec_id, "agent": agent,
                 "archived": {"agent": syn.agent, "verdict": syn.archived_verdict,
                              "spec_status": syn.status,
                              "attempts": syn.attempts_total,
                              "prompt_tokens": syn.payload.get("prompt_tokens")},
                 "calls": [], "tests": []}
    base = scratch / syn.spec_id / agent
    try:
        workspace_root = base / "ws"
        build_scratch_workspace(syn.spec_id, syn.attempts_total,
                                workspace_root / syn.spec_id, archive=archive_specs)
        db_path = build_scratch_db(syn.spec_id, syn.started_at,
                                   base / "tasks.sqlite", source=archive_db)
    except Unreproducible as exc:
        return {**row, "status": "unreproducible", "reason": str(exc)}

    db = Database(db_path=db_path, workspace_root=workspace_root)
    try:
        spec = db.get_spec(syn.spec_id)
        impl = [t for t in db.list_tasks_for_spec(syn.spec_id)
                if t.role == "implementer"]
        if spec is None or not impl:
            return {**row, "status": "unreproducible",
                    "reason": "no implementer task before synthesis started"}
        impl_task = impl[-1]
        spec_dir = db.spec_dir(spec.id)
        ts = d._load_plan(spec).get("test_strategy")
        ts = ts if isinstance(ts, dict) else {}
        framework = ts.get("framework", "pytest")
        framework_opts = {k: v for k, v in ts.items()
                          if k not in ("framework", "required")}
        repo = framework_opts.get("repo")
        overlays_repo = repo == REPO.name and framework not in MAC_FRAMEWORKS
        ref = pinned_ref(framework, repo, spec_dir / "context.json", syn.started_at)
        row.update(framework=framework, repo=repo, pinned_ref=ref)
        if not prompt_only and ref is None and (framework in MAC_FRAMEWORKS
                                                or overlays_repo):
            return {**row, "status": "unreproducible",
                    "reason": "no recorded commit to pin the tests to"}
        if framework in MAC_FRAMEWORKS:
            framework_opts["base_ref"] = ref

        def fake_call(role, messages, **kw):
            record = {"phase": "synthesis" if not row["calls"] else "repair",
                      "prompt_sha256": prompt_hash(messages),
                      "prompt_chars": sum(len(m.get("content") or "")
                                          for m in messages)}
            row["calls"].append(record)
            if prompt_only:
                raise _PromptOnly()
            meta = kw.get("meta")
            kw["agent"] = agent
            t0 = time.monotonic()
            try:
                return real_call(role, messages, **kw)
            finally:
                record["wall_s"] = round(time.monotonic() - t0, 1)
                if isinstance(meta, dict):
                    record.update({k: meta.get(k) for k in (
                        "finish_reason", "prompt_tokens", "completion_tokens",
                        "rag") if k in meta})

        def recording_guard(spec_id, sdir, fw, opts, **kw):
            passed, output = real_guard(spec_id, sdir, fw, opts, **kw)
            row["tests"].append(_test_counts(output, fw, passed))
            return passed, output

        def pinned_archive(repo_root, into):
            out = subprocess.run(
                ["git", "-C", str(repo_root), "archive", "--format=tar", ref, "src"],
                capture_output=True, check=True, timeout=60)
            into.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
                tar.extractall(path=into, filter="data")

        real_call, real_guard = d.call_agent, d._run_tests_with_guard
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(d, "_SYNTHESIS_AGENT", BUDGET_AGENT))
            stack.enter_context(mock.patch.object(d, "call_agent", fake_call))
            stack.enter_context(mock.patch.object(d, "_run_tests_with_guard",
                                                  recording_guard))
            stack.enter_context(mock.patch.object(
                test_runner, "fetch_repo_files",
                archived_fetch(spec_dir / "context.json")))
            if ref is not None and overlays_repo:
                stack.enter_context(mock.patch.object(
                    test_runner, "_extract_committed_src", pinned_archive))
            try:
                passed, _output = d._run_synthesis(
                    db, spec, impl_task, spec_dir, framework, framework_opts)
            except d.SynthesisNoVerdict as exc:
                if prompt_only and row["calls"]:
                    return {**row, "status": "prompt_only"}
                return {**row, "status": "no_verdict",
                        "reason": str(getattr(exc, "failure", exc))[:500]}
        if not row["tests"]:
            # The response never reached the tests: unparseable, or empty.
            return {**row, "status": "parse_failed"}
        row["synthesis"] = row["tests"][0]
        row["final"] = row["tests"][-1] | {"passed": passed}
        return {**row, "status": "scored"}
    finally:
        db.close_all()


# ── the command ──────────────────────────────────────────────────────────────

def select(syntheses: dict[str, list[Synthesis]], specs: list[str] | None,
           population: str) -> list[Synthesis]:
    """The last synthesis of each chosen spec: the one its verdict came from."""
    chosen = []
    for spec_id, calls in syntheses.items():
        if specs and spec_id not in specs:
            continue
        if not specs and population == "done" and calls[-1].status != "done":
            continue
        chosen.append(calls[-1])
    return chosen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("agent", help="roster agent to synthesize with")
    ap.add_argument("--specs", nargs="*", help="spec ids (default: the population)")
    ap.add_argument("--population", choices=("done", "all"), default="all",
                    help="'done' is the positive control's population")
    ap.add_argument("--prompt-only", action="store_true",
                    help="rebuild and hash the prompts; no model call, no tests")
    ap.add_argument("--out", type=Path, default=REPO / "var" / "replay_synthesis")
    ap.add_argument("--archive", type=Path, default=ARCHIVE_DB.parent,
                    help="the tasks_db directory to replay from (read only)")
    args = ap.parse_args(argv)
    archive_db, archive_specs = args.archive / ARCHIVE_DB.name, args.archive / "specs"

    load_env()
    args.out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        filename=str(args.out / f"{args.agent}.log"))
    rows_path = args.out / f"{args.agent}{'.prompts' if args.prompt_only else ''}.jsonl"
    chosen = select(find_syntheses(archive_db), args.specs, args.population)
    print(f"{len(chosen)} spec(s) → {rows_path}")
    with rows_path.open("a") as out:
        for syn in chosen:
            try:
                row = replay(syn, args.agent, args.out / "scratch",
                             prompt_only=args.prompt_only, archive_db=archive_db,
                             archive_specs=archive_specs)
            except Exception as exc:  # a harness fault, reported, never a score
                logger.exception("replay of %s crashed", syn.spec_id)
                row = {"spec": syn.spec_id, "agent": args.agent,
                       "status": "harness_error", "reason": repr(exc)[:500]}
            row["replayed_at"] = datetime.now().isoformat(timespec="seconds")
            out.write(json.dumps(row) + "\n")
            out.flush()
            final = row.get("final") or {}
            print(f"{syn.spec_id} {row['status']:<15} archived={syn.archived_verdict:<7} "
                  f"replay={final.get('passed', '-')} "
                  f"{final.get('passed_n', '')}/{final.get('total', '')} "
                  f"{row.get('reason', '')[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
