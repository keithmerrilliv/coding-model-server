#!/usr/bin/env python3
"""coding-model-orchestrator daemon.

Long-running process that drives autonomous-mode specs through their state
machine. The full pipeline (plan → architect → implementer → reviewer →
supervisor retry/replan) lives here.

State transitions:

    PENDING_PLAN ──┬──> NEEDS_CLARIFICATION  (planner asked questions)
                   ├──> PLAN_REVIEW           (planner produced YAML)
                   └──> FAILED                (planner output unparseable)

    NEEDS_CLARIFICATION ──┬──> PENDING_PLAN   (clarification gate approved
                          │                    → re-run planner with answers)
                          └──> CANCELLED       (clarification gate rejected)

    PLAN_REVIEW ──┬──> EXECUTING               (plan approved)
                  └──> PENDING_PLAN            (plan rejected → planner re-runs
                                                with rejection notes as a
                                                clarification round)

    EXECUTING ──> [architect → review-gate → implementer → review-gate →
                   reviewer → review-gate → COMPLETED, with supervisor-
                   driven retries on review rejection or test failure]

The daemon talks to the coding_model_autonomous SQLite store directly (it shares
the file with coding-model-server) and to the coding-model-server inference HTTP API for
calling each agent. It must NOT serve HTTP itself.
"""
from __future__ import annotations

import logging
import json
import os
import re
import shutil
import signal
import threading
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import requests
from dotenv import load_dotenv

try:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except OSError:
    # Unreadable is fine and expected under systemd: the unit already
    # delivers these values via EnvironmentFile (read by the manager BEFORE
    # the namespace is set up), and the .env paths are listed in
    # InaccessiblePaths so LLM-authored test code can't read the secrets if
    # the bwrap sandbox is bypassed (DEV-174). This call is the fallback for
    # manual runs; a PermissionError here must not kill the daemon at import.
    pass

# Transport errors that mean "couldn't reach the server" rather than "the work
# failed". _http already backs off and retries these; if one still escapes, the
# server was down longer than the backoff window (e.g. a slow redeploy), so the
# spec/task is left re-runnable instead of being FAILED and its approved work
# discarded. HTTPError (a real 4xx/5xx the server returned) is deliberately NOT
# here — that is a genuine failure and still fails the spec.
_TRANSPORT_ERRORS = (requests.ConnectionError, requests.Timeout)

from coding_model_autonomous import (
    ArtifactKind,
    Database,
    EventKind,
    GateStatus,
    GateType,
    SpecStatus,
    TaskStatus,
)
from coding_model_autonomous.models import ReviewGate, Spec, Task
from coding_model_autonomous.planner import (
    PlannerClarify,
    PlannerYaml,
    call_planner,
)
from coding_model_autonomous.jira_client import (
    AtlassianApiJiraClient,
    FakeJiraClient,
    JiraClient,
)
from coding_model_autonomous.jira_sync import JiraSync
from coding_model_autonomous import (
    adversarial, design_testability, executor, test_runner,
)
from coding_model_autonomous.test_runner import run_tests
from coding_model_autonomous.retry_policy import (
    _PRESERVE_ON_RETRY,
    _clean_spec_dir_for_retry,
    _latest_supervisor_feedback,
    _load_prior_decisions,
    _read_retry_attempts,
    _rotation_pick,
    _select_implementer_agent,
    _snapshot_retry,
)
from coding_model_autonomous.executor import (
    ImplementerResult,
    MAX_RETRIES,
    ParseError,
    _write_artifact,
    build_architect_message,
    build_implementer_message,
    build_manifest_message,
    build_per_file_message,
    build_reviewer_message,
    build_synthesis_message,
    call_agent,
    parse_architect_response,
    parse_implementer_response,
    parse_manifest_response,
    parse_reviewer_response,
    summarize_written_files,
)
from coding_model_autonomous import supervisor as _supervisor

# ── Configuration ────────────────────────────────────────────────────────────

POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "5"))
# How often to re-announce gates still waiting on a human (DEV-430). Well
# above POLL_INTERVAL on purpose: the point is a periodic reminder, not a
# per-tick log line.
GATE_REPORT_INTERVAL = float(os.getenv("ORCHESTRATOR_GATE_REPORT_INTERVAL", "300"))
# Per-spec worker pool size (DEV-393). Small on purpose: agent calls all
# queue on the same llama-server anyway, so extra workers buy nothing but
# GPU-queue depth — the win is that cheap state transitions (gate approvals,
# bootstraps) no longer wait behind another spec's long agent call.
SPEC_WORKERS = int(os.getenv("ORCHESTRATOR_SPEC_WORKERS", "4"))
LOG_LEVEL = os.getenv("ORCHESTRATOR_LOG_LEVEL", "INFO").upper()
SUPERVISOR_ENABLED = os.getenv("AUTONOMOUS_SUPERVISOR", "0") == "1"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - orchestrator - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")


# ── Gate prompt formatting ───────────────────────────────────────────────────

def _format_plan_gate_prompt(spec: Spec, yaml_text: str) -> str:
    return (
        f"## Plan ready for review: {spec.title}\n\n"
        f"Spec ID: `{spec.id}`\n\n"
        f"The planner has produced the following plan. Approve to begin "
        f"execution, or reject with notes to ask for changes (the planner "
        f"will re-run with your notes).\n\n"
        f"```yaml\n{yaml_text}\n```\n"
    )


def _format_clarification_gate_prompt(spec: Spec, questions: list[str]) -> str:
    """Markdown a human will see when reviewing a clarification gate.

    The numbered questions inside the fenced code block are the canonical
    form: ``_collect_clarification_rounds`` parses them back out on re-run.
    """
    qblock = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    return (
        f"## Clarification needed: {spec.title}\n\n"
        f"Spec ID: `{spec.id}`\n\n"
        f"The planner needs more information before producing a plan. "
        f"Approve this gate with `--notes` containing your answers (numbered "
        f"or freeform — the planner will read them in context). Reject the "
        f"gate to cancel the spec.\n\n"
        f"### Questions\n\n"
        f"```\n{qblock}\n```\n"
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _collect_clarification_rounds(db: Database, spec_id: str) -> list[tuple[str, str]]:
    """Build the (questions, answers) round list for a planner re-run.

    Walks every clarification gate for this spec in chronological order and
    returns the ones that have a recorded answer. Pending gates are skipped
    — they're what we'd be waiting on, so they cannot be a "previous round."
    """
    rounds: list[tuple[str, str]] = []
    for gate in db.list_gates_for_spec(spec_id, GateType.CLARIFICATION):
        if gate.reviewer_notes is None or gate.status != GateStatus.APPROVED:
            continue
        rounds.append((gate.prompt_md, gate.reviewer_notes))
    return rounds


def _latest_gate_of_type(db: Database, spec_id: str,
                         gate_type: GateType) -> ReviewGate | None:
    gates = db.list_gates_for_spec(spec_id, gate_type)
    return gates[-1] if gates else None


def _note_truncation(db: Database, spec: Spec, task, role: str,
                     meta: dict, max_tokens: int) -> None:
    """Record an OUTPUT_TRUNCATED event when an agent response hit max_tokens.

    ``meta`` is the dict ``call_agent`` populated. A finish_reason of "length"
    means the model was cut off mid-output — for the implementer/synthesizer
    that usually leaves the last ``<<<FILE>>>`` block unterminated, which the
    reviewer then reports as a (phantom) "missing file" FAIL. Surfacing
    truncation as its own event lets the operator see the real cause instead
    of chasing the false negative, and gives the stats layer a signal to tune
    the budget against.
    """
    if not meta.get("truncated"):
        return
    agent = meta.get("agent")
    db.record_event(
        EventKind.OUTPUT_TRUNCATED,
        spec_id=spec.id,
        task_id=getattr(task, "id", None),
        payload={"role": role, "agent": agent, "max_tokens": max_tokens,
                 "finish_reason": meta.get("finish_reason")},
    )
    # NB: raising the budget is usually the WRONG fix. A model that hits
    # max_tokens on a small file is generating degenerately (e.g. a reasoning
    # model exhausting the budget inside <think>) — a bigger budget just feeds a
    # longer loop. Check `agent` first; the per-file path now rotates instead.
    logger.warning(
        "spec %s: agent=%s %s output truncated at max_tokens=%d "
        "(finish_reason=length) — possible degenerate generation; check the "
        "agent before raising the budget",
        spec.id, agent, role, max_tokens,
    )


# ── State machine handlers ───────────────────────────────────────────────────

def _process_pending_plan(db: Database, spec: Spec) -> None:
    """Run the planner agent against a spec in PENDING_PLAN state.

    Three possible outcomes:
      * PlannerYaml      → spec → PLAN_REVIEW + plan_approval gate
      * PlannerClarify   → spec → NEEDS_CLARIFICATION + clarification gate
      * PlannerError     → spec → FAILED with the parse error in the event log
    """
    spec_dir = db.spec_dir(spec.id)
    md_path = spec_dir / spec.source_md_path
    if not md_path.exists():
        logger.error("spec %s: source markdown missing at %s",
                     spec.id, md_path)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    markdown = md_path.read_text()
    rounds = _collect_clarification_rounds(db, spec.id)
    logger.info("spec %s: running planner (md=%d bytes, rounds=%d)",
                spec.id, len(markdown), len(rounds))

    try:
        result = call_planner(markdown, clarifications=rounds)
    except _TRANSPORT_ERRORS as e:
        # Couldn't reach the server (redeploy race, or a read timeout). Leave the
        # spec in PENDING_PLAN so the next tick re-runs the planner rather than
        # discarding it. Recorded, not FAILED.
        logger.warning("spec %s: planner call hit transport error (%s) — "
                       "leaving PENDING_PLAN for retry", spec.id, type(e).__name__)
        db.record_event(
            EventKind.PLANNER_RAN,
            spec_id=spec.id,
            payload={"transient_error": f"{type(e).__name__}: {e}"},
        )
        return
    except Exception as e:
        logger.exception("spec %s: planner call failed", spec.id)
        db.record_event(
            EventKind.PLANNER_RAN,
            spec_id=spec.id,
            payload={"error": f"{type(e).__name__}: {e}"},
        )
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    db.record_event(
        EventKind.PLANNER_RAN,
        spec_id=spec.id,
        payload={
            "result_kind": type(result).__name__,
            "rounds_provided": len(rounds),
        },
    )

    if isinstance(result, PlannerYaml):
        _accept_plan(db, spec, spec_dir, result)
    elif isinstance(result, PlannerClarify):
        _emit_clarification(db, spec, result)
    else:
        # PlannerError
        logger.error("spec %s: planner output unparseable: %s",
                     spec.id, result.reason)
        db.record_event(
            EventKind.PLANNER_RAN,
            spec_id=spec.id,
            payload={
                "error": result.reason,
                "raw_excerpt": result.raw_response[:500],
            },
        )
        db.update_spec_status(spec.id, SpecStatus.FAILED)


# DEV-426: keys each Apple framework needs before a dispatch can even be built.
# A plan missing these is invalid by construction — it cannot run, and the
# failure surfaces at the test phase, long after design and implementation.
_FRAMEWORK_REQUIRED_KEYS = {
    "swift_test": ("repo",),
    "xcodebuild_test": ("repo", "scheme", "filter"),
}
# How many times validation may bounce a plan back before the spec fails, so a
# planner that cannot produce a valid block does not loop forever.
PLAN_VALIDATION_MAX_ROUNDS = int(
    os.getenv("AUTONOMOUS_PLAN_VALIDATION_MAX_ROUNDS", "2"))
_AUTO_PLAN_REJECT_MARKER = "## Plan validation failure (DEV-426)"
_SPEC_TEST_STRATEGY_RE = re.compile(
    r"^##+\s*test_strategy\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE)


def _spec_declared_test_strategy(spec_md: str) -> dict:
    """The `## test_strategy` block from the spec itself, as a mapping.

    Specs write it as an indented YAML block under the heading. Returns {} when
    absent or unparseable — this is an advisory source, never a hard failure.
    """
    import yaml as _yaml
    if not spec_md:
        return {}
    match = _SPEC_TEST_STRATEGY_RE.search(spec_md)
    if not match:
        return {}
    block = textwrap.dedent(match.group(1)).strip()
    if not block:
        return {}
    try:
        parsed = _yaml.safe_load(block)
    except _yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_test_strategy(yaml_text: str, spec_md: str) -> list[str]:
    """Problems that make a plan's test_strategy unrunnable. Empty means fine.

    Two rules. The framework's own required keys must be present, because
    without them no dispatch can be constructed. And every key the spec's own
    test_strategy block declares must survive into the plan — the planner may
    add keys, never silently drop them. The second rule is the stronger one:
    `base_ref` and `protected_paths` are not framework-required, and losing
    them fails silently rather than loudly (DEV-427 is disabled outright).
    """
    import yaml as _yaml
    try:
        plan = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError:
        return []  # malformed YAML is _bootstrap_tasks' job to reject, not ours
    if not isinstance(plan, dict):
        return []
    strategy = plan.get("test_strategy")
    if not isinstance(strategy, dict):
        return []  # no strategy at all is a different (non-Apple) shape

    problems: list[str] = []
    reported: set[str] = set()
    framework = str(strategy.get("framework") or "").strip()
    for key in _FRAMEWORK_REQUIRED_KEYS.get(framework, ()):
        if not strategy.get(key):
            reported.add(key)
            problems.append(
                f"`{key}` is required for `framework: {framework}` and is missing. "
                f"Without it the runner dispatch cannot be built at all.")

    # A key can fail both rules; say so once.
    declared = _spec_declared_test_strategy(spec_md)
    dropped = [k for k in declared
               if k not in ("framework", "required", "notes")
               and k not in strategy and k not in reported]
    for key in dropped:
        problems.append(
            f"`{key}` is declared in the spec's own test_strategy block and is "
            f"absent from the plan. Copy it through as a real YAML key — "
            f"prose inside `notes` is never parsed.")
    return problems


def _reject_plan_for_validation(db: Database, spec: Spec, problems: list[str],
                                yaml_text: str) -> bool:
    """Bounce an invalid plan back to the planner. True if the round was issued.

    Uses the same channel a human rejection uses — an approved CLARIFICATION
    gate carrying the feedback, then PENDING_PLAN — so the planner sees it as
    a normal revision round and no operator attention is consumed.
    """
    prior = sum(1 for g in db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
                if (g.prompt_md or "").startswith(_AUTO_PLAN_REJECT_MARKER))
    if prior >= PLAN_VALIDATION_MAX_ROUNDS:
        logger.error("spec %s: plan still invalid after %d validation round(s) "
                     "— failing: %s", spec.id, prior, "; ".join(problems))
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return False

    bullets = "\n".join(f"- {p}" for p in problems)
    notes = (
        "## Plan rejection feedback\n\n"
        "The plan's `test_strategy` cannot be dispatched as written. This was "
        "caught automatically before review, so no reviewer has seen it yet.\n\n"
        f"{bullets}\n\n"
        "Re-emit the plan with these as structured keys under `test_strategy`. "
        "Keep whatever prose you have in `notes` — it is useful to the "
        "implementer — but the values must also exist as keys, because the "
        "runner dispatch is built from the keys and `notes` is never parsed.\n"
    )
    gate = db.create_gate(
        spec_id=spec.id,
        gate_type=GateType.CLARIFICATION,
        prompt_md=_AUTO_PLAN_REJECT_MARKER,
    )
    db.respond_to_gate(gate.id, "approved", notes=notes)
    db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)
    logger.info("spec %s: plan failed validation (round %d/%d), replanning: %s",
                spec.id, prior + 1, PLAN_VALIDATION_MAX_ROUNDS,
                "; ".join(problems))
    return True


# DEV-492: the pipeline can create files; it cannot modify one. The implementer
# never receives existing sources — build_implementer_message takes spec, design,
# rejection notes and clarifications, and the Mac runner exposes no read path — so
# a file marked "modify" is re-emitted from imagination and written over the real
# thing. On spec_f47132ab that replaced ~120 lines of working UI with a 45-line
# reconstruction; it was caught only because the invention happened not to compile.
#
# Until the read path exists, refuse rather than corrupt. Set this to "1" to
# override once the implementer is actually given the files.
ALLOW_UNREAD_FILE_MODIFICATION = (
    os.getenv("AUTONOMOUS_ALLOW_UNREAD_FILE_MODIFICATION", "0") == "1"
)

# Matches a spec's change-surface table row: | `path` | modify (...) |
#
# Deliberately keyed on the TABLE rather than on the word "modify" anywhere in
# the prose. Greenfield specs routinely say "extend an existing package" or
# "do not modify Package.swift" while every file they write is new — the
# Centipede spec does both and must not be blocked. A change-surface row is an
# explicit declaration that an existing file is an output.
_CHANGE_SURFACE_ROW = re.compile(
    r"^\|\s*`?([^`|]+?)`?\s*\|\s*(?:\*\*)?(modif\w*)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _declared_file_modifications(spec_md: str) -> list[str]:
    """Paths a spec's change-surface table marks as modified, not created."""
    if not spec_md:
        return []
    return [
        m.group(1).strip()
        for m in _CHANGE_SURFACE_ROW.finditer(spec_md)
        if m.group(1).strip() and m.group(1).strip().lower() != "path"
    ]


def _unreadable_declared_modifications(
    spec: Spec, yaml_text: str, spec_md: str
) -> list[str]:
    """Declared-modify paths the pipeline still cannot read (DEV-492).

    Empty means every file the spec says it will modify can be fetched at
    base_ref, so the implementer will be editing rather than inventing and the
    plan is safe to run. Non-empty is the original hazard and still blocks.

    Reads the plan from *yaml_text* rather than spec.normalized_yaml: at plan
    acceptance the plan has not been persisted yet.
    """
    import yaml as _yaml

    paths = _declared_file_modifications(spec_md)
    if not paths:
        return []
    try:
        plan = _yaml.safe_load(yaml_text) or {}
        strategy = plan.get("test_strategy")
    except _yaml.YAMLError:
        strategy = None
    if not isinstance(strategy, dict) or not strategy.get("repo"):
        # No registered repo to read from — unchanged from before the read path.
        return paths
    try:
        files, _ = test_runner.fetch_repo_files(
            strategy["repo"], paths, strategy.get("base_ref") or "HEAD")
    except Exception as e:
        logger.warning("spec %s: could not probe declared modifications (%s)",
                       spec.id, e)
        return paths
    got = {p for p, _ in files}
    return [p for p in paths if p not in got]


def _drop_undeliverable_manifest_entries(spec: Spec, entries: list) -> list:
    """Remove manifest entries that can never be delivered (DEV-499).

    In manifest mode each file costs its own agent call, so an entry on
    `protected_paths` is a guaranteed-wasted call: DEV-427 strips those paths
    before dispatch, the worktree keeps base_ref's copy, and whatever was
    generated is discarded. On Centipede run 5 that was 1 of 7 calls.

    It also corrupts a signal. The runner banners every off-limits file the
    implementer touched, which is how a reviewer spots an implementer going out
    of bounds — but when the manifest *told* it to write the file, the banner
    reports a violation that never happened and buries the real ones.

    Filtered here rather than in the manifest prompt because a prompt rule is
    advisory and this one has to hold.
    """
    strategy = _load_plan(spec).get("test_strategy")
    protected = set()
    if isinstance(strategy, dict):
        protected = {str(p).strip() for p in (strategy.get("protected_paths") or []) if p}
    if not protected:
        return entries
    kept, dropped = [], []
    for e in entries:
        (dropped if e.path.strip() in protected else kept).append(e)
    if dropped:
        # Logged, never silent: _verify_manifest_workspace (DEV-106) treats a
        # manifest-declared file missing from the workspace as an anomaly, and
        # this is a deliberate omission rather than that.
        logger.info("spec %s: dropped %d protected path(s) from the manifest — "
                    "they cannot be delivered and each would cost a generation "
                    "call: %s", spec.id, len(dropped),
                    ", ".join(e.path for e in dropped))
    return kept


def _fetch_protected_files_for_spec(spec: Spec) -> list[tuple[str, str]]:
    """Read-only contents of the spec's `protected_paths` (DEV-492 / DEV-427).

    Protected files are dropped before dispatch so the pipeline cannot write
    them — but they are still compiled into the target, and neither the
    architect nor the implementer has ever been able to see what they declare.
    Centipede run 5 died on `invalid redeclaration of 'Field'` for exactly that
    reason: the design created a type the protected scaffold already had.

    Showing them cannot widen what the pipeline may change, since the write
    path drops these paths regardless of what any role produces.
    """
    strategy = _load_plan(spec).get("test_strategy")
    if not isinstance(strategy, dict):
        return []
    repo = strategy.get("repo")
    paths = [p for p in (strategy.get("protected_paths") or []) if p]
    if not repo or not paths:
        return []
    try:
        files, problems = test_runner.fetch_repo_files(
            repo, paths, strategy.get("base_ref") or "HEAD")
    except Exception as e:
        logger.warning("spec %s: protected-file read failed (%s); roles will "
                       "not see what those files declare", spec.id, e)
        return []
    for problem in problems:
        logger.warning("spec %s: protected-file read problem — %s", spec.id, problem)
    if files:
        logger.info("spec %s: supplied %d protected file(s) as read-only "
                    "context: %s", spec.id, len(files),
                    ", ".join(p for p, _ in files))
    return files


def _fetch_existing_files_for_spec(spec: Spec, spec_md: str) -> list[tuple[str, str]]:
    """Current contents of the files this spec marks as modified (DEV-492).

    Returns [] for the greenfield case (no change-surface rows), for local
    frameworks whose repo is not on the Mac, and whenever the read path is
    unavailable — the implementer then behaves exactly as it did before this
    existed. Never raises: a spec must not fail because a git read failed.
    """
    paths = _declared_file_modifications(spec_md)
    if not paths:
        return []
    strategy = _load_plan(spec).get("test_strategy")
    if not isinstance(strategy, dict):
        return []
    repo = strategy.get("repo")
    if not repo:
        # No registered repo means no runner-side checkout to read from; this
        # is the local-framework case (pytest/node), where the spec dir is the
        # whole world and there is nothing to fetch.
        return []
    base_ref = strategy.get("base_ref") or "HEAD"
    try:
        files, problems = test_runner.fetch_repo_files(repo, paths, base_ref)
    except Exception as e:  # never let a read failure kill the spec
        logger.warning("spec %s: existing-file read failed (%s); the "
                       "implementer will not see the files it must modify",
                       spec.id, e)
        return []
    for problem in problems:
        logger.warning("spec %s: existing-file read problem — %s",
                       spec.id, problem)
    if files:
        logger.info("spec %s: supplied %d existing file(s) to the implementer: %s",
                    spec.id, len(files), ", ".join(p for p, _ in files))
    else:
        logger.warning("spec %s: %d file(s) marked modify but none could be "
                       "read — implementer is working blind", spec.id, len(paths))
    return files


def _block_plan_for_unreadable_modification(
    db: Database, spec: Spec, paths: list[str]
) -> None:
    """Fail the spec instead of letting it overwrite files it has never read.

    NOT routed through _reject_plan_for_validation: that hands the plan back to
    the planner for another round, and replanning cannot give the pipeline an
    ability it structurally lacks — it would just loop until the round budget
    ran out. This is a hard stop that needs a human.
    """
    bullets = "\n".join(f"- `{p}`" for p in paths)
    logger.error(
        "spec %s: plan declares %d existing file(s) as modified but the "
        "implementer cannot be given their contents (DEV-492) — failing rather "
        "than overwriting: %s", spec.id, len(paths), ", ".join(paths))
    gate = db.create_gate(
        spec_id=spec.id,
        gate_type=GateType.CLARIFICATION,
        prompt_md=(
            "## Blocked — this spec modifies files the implementer cannot read\n\n"
            "The change surface marks these as **modify**:\n\n"
            f"{bullets}\n\n"
            "The implementer is never given existing file contents, so it would "
            "re-emit each of these from the spec and design alone and the runner "
            "would write that reconstruction over the real file. Where the "
            "invention happens to compile, the loss is silent.\n\n"
            "This is structural (DEV-492), not a prompt problem — no amount of "
            "instruction can make an agent preserve what it has never seen.\n\n"
            "**Options:** rewrite the spec so every output is a new file, wait "
            "for the runner read path, or set "
            "`AUTONOMOUS_ALLOW_UNREAD_FILE_MODIFICATION=1` to accept the risk "
            "deliberately."
        ),
    )
    db.respond_to_gate(gate.id, "rejected", notes="blocked by DEV-492 guard")
    db.update_spec_status(spec.id, SpecStatus.FAILED)


def _accept_plan(db: Database, spec: Spec, spec_dir, result: PlannerYaml) -> None:
    """Persist a YAML plan to disk and create the plan_approval gate."""
    yaml_text = result.yaml_text

    # DEV-426: a plan that cannot dispatch must never reach a human gate. Three
    # consecutive runs of the same spec lost test_strategy keys into `notes`
    # prose; each cost a review round, and a dropped `protected_paths` silently
    # disables the scaffold protection with no error at all.
    try:
        spec_md_path = spec_dir / spec.source_md_path
        spec_md = spec_md_path.read_text() if spec_md_path.exists() else ""
    except OSError:
        spec_md = ""
    problems = _validate_test_strategy(yaml_text, spec_md)
    if problems:
        _reject_plan_for_validation(db, spec, problems, yaml_text)
        return

    # Checked before the human gate for the same reason DEV-426 moved test_strategy
    # validation here: an operator should not spend a review round approving a plan
    # the pipeline cannot execute without destroying work.
    # The block is now conditional on the files actually being unreadable. When
    # DEV-492's read path can serve them the pipeline is no longer guessing, so
    # refusing would block precisely the specs the read path exists to enable.
    # Probing here doubles as a pre-flight: a change-surface row naming a path
    # that does not exist at base_ref is caught before a human reviews the plan,
    # not after the implementer has written an invention over it.
    if not ALLOW_UNREAD_FILE_MODIFICATION:
        unreadable = _unreadable_declared_modifications(spec, yaml_text, spec_md)
        if unreadable:
            _block_plan_for_unreadable_modification(db, spec, unreadable)
            return

    db.update_spec_status(
        spec.id,
        SpecStatus.PLAN_REVIEW,
        normalized_yaml=yaml_text,
    )
    yaml_path = spec_dir / "plan.yaml"
    yaml_path.write_text(yaml_text + "\n")
    db.create_artifact(
        spec_id=spec.id,
        kind=ArtifactKind.SPEC_YAML,
        path="plan.yaml",
    )
    db.create_gate(
        spec_id=spec.id,
        gate_type=GateType.PLAN_APPROVAL,
        prompt_md=_format_plan_gate_prompt(spec, yaml_text),
    )
    logger.info("spec %s: plan accepted (%d bytes), plan_approval gate created",
                spec.id, len(yaml_text))


def _emit_clarification(db: Database, spec: Spec,
                        result: PlannerClarify) -> None:
    """Create a clarification gate and park the spec until the human responds."""
    db.update_spec_status(spec.id, SpecStatus.NEEDS_CLARIFICATION)
    db.create_gate(
        spec_id=spec.id,
        gate_type=GateType.CLARIFICATION,
        prompt_md=_format_clarification_gate_prompt(spec, result.questions),
    )
    logger.info("spec %s: planner needs clarification (%d questions)",
                spec.id, len(result.questions))


def _process_needs_clarification(db: Database, spec: Spec) -> None:
    """Check the latest clarification gate; if resolved, advance the spec.

    Approved → return to PENDING_PLAN so the planner re-runs with the
    answers in the next tick. Rejected → mark the spec CANCELLED.
    """
    gate = _latest_gate_of_type(db, spec.id, GateType.CLARIFICATION)
    if gate is None:
        logger.warning("spec %s: NEEDS_CLARIFICATION but no clarification "
                       "gate exists; marking failed", spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if gate.status == GateStatus.PENDING:
        return  # waiting on the human

    if gate.status == GateStatus.APPROVED:
        if not gate.reviewer_notes:
            logger.warning("spec %s: clarification approved with no notes; "
                           "planner will re-run without new context", spec.id)
        logger.info("spec %s: clarification answered, returning to "
                    "PENDING_PLAN for replanning", spec.id)
        db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)
    elif gate.status == GateStatus.REJECTED:
        logger.info("spec %s: clarification rejected, cancelling spec",
                    spec.id)
        db.update_spec_status(spec.id, SpecStatus.CANCELLED)


def _process_plan_review(db: Database, spec: Spec) -> None:
    """Look for a resolved plan_approval gate and act on it."""
    gate = _latest_gate_of_type(db, spec.id, GateType.PLAN_APPROVAL)
    if gate is None:
        logger.warning("spec %s: PLAN_REVIEW without a plan_approval gate; "
                       "marking failed", spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if gate.status == GateStatus.PENDING:
        return  # waiting on the human

    if gate.status == GateStatus.APPROVED:
        logger.info("spec %s: plan approved, transitioning to EXECUTING", spec.id)
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    elif gate.status == GateStatus.REJECTED:
        # The reviewer's rejection notes become a synthetic clarification
        # round so the planner sees them on its next pass.
        logger.info("spec %s: plan rejected, replanning with notes=%r",
                    spec.id, gate.reviewer_notes)
        if gate.reviewer_notes:
            new_gate = db.create_gate(
                spec_id=spec.id,
                gate_type=GateType.CLARIFICATION,
                prompt_md=("## Plan rejection feedback\n\n"
                           "The reviewer rejected the previous plan with "
                           "the following notes — treat them as new "
                           "requirements and revise."),
            )
            db.respond_to_gate(new_gate.id, "approved",
                               notes=gate.reviewer_notes)
        db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)


# ── Main loop ────────────────────────────────────────────────────────────────

class _ShutdownFlag:
    """Shared flag flipped by SIGTERM/SIGINT handlers."""
    def __init__(self) -> None:
        self.set = False

    def install_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum, frame):
        logger.info("received signal %d, shutting down after this tick", signum)
        self.set = True


# Module-level so long-running work deep inside a tick (the per-file
# manifest chain) can notice a pending SIGTERM between agent calls instead
# of letting systemd's stop timeout escalate to SIGKILL mid-run (DEV-141).
_shutdown_flag = _ShutdownFlag()


class ShutdownRequested(RuntimeError):
    """Raised between per-file manifest calls when SIGTERM arrived; the
    task is reset to PENDING so the next daemon start re-runs it."""


class SpecScheduler:
    """Runs each spec's per-tick pass on its own worker (DEV-393).

    The tick loop used to call every processor synchronously, so one spec's
    long agent call froze every other spec — an observed retries-exhausted
    synthesis (~195k-token prompt, ~8 min of prefill before generation even
    started) left an already-approved plan gate unprocessed for 12+ minutes,
    and from the outside the starved queue was indistinguishable from a hang.

    One pass per spec at a time (the in-flight registry) is the only ordering
    the state machine needs; across specs the DB is the shared state, and its
    WAL + thread-local connections already serve the heartbeat and Jira-sync
    threads.

    Not thread-safe by design: submit/reap/drain run on the tick thread only.
    """

    # How often reap() names a still-busy spec in the log.
    BUSY_LOG_INTERVAL = 60.0

    def __init__(self, max_workers: "int | None" = None):
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers or SPEC_WORKERS,
            thread_name_prefix="spec-worker",
        )
        # spec_id -> (future, pass label, monotonic start, last busy-log)
        self._in_flight: dict[str, list] = {}

    def submit(self, spec_id: str, label: str, fn, /, *args) -> bool:
        """Queue one pass for a spec; refused while a pass is in flight."""
        if spec_id in self._in_flight:
            return False
        fut = self._pool.submit(fn, *args)
        now = time.monotonic()
        self._in_flight[spec_id] = [fut, label, now, now]
        return True

    def reap(self) -> None:
        """Drop finished passes and name the still-busy specs in the log, so
        a starved queue reads differently from a crashed daemon."""
        now = time.monotonic()
        for spec_id, entry in list(self._in_flight.items()):
            fut, label, started, last_logged = entry
            if fut.done():
                del self._in_flight[spec_id]
                exc = fut.exception()
                if exc is not None:
                    # _run_spec_pass catches processor errors; anything that
                    # still escaped (e.g. the FAILED write itself) surfaces
                    # here instead of dying silently in the Future.
                    logger.error("spec %s: %s pass crashed: %r",
                                 spec_id, label, exc)
            elif now - last_logged >= self.BUSY_LOG_INTERVAL:
                logger.info("spec %s: %s pass still running (%.0fs)",
                            spec_id, label, now - started)
                entry[3] = now

    def drain(self) -> None:
        """Wait for in-flight passes — the shutdown contract is unchanged:
        SIGTERM means 'after the current work', and the long manifest chain
        still bails early via ShutdownRequested."""
        self._pool.shutdown(wait=True)


def tick(db: Database, scheduler: "SpecScheduler | None" = None) -> None:
    """One pass over the spec table. Idempotent — safe to call any time.

    With a scheduler (the daemon), each spec's pass runs on its own worker
    so one spec's long agent call cannot starve the rest (DEV-393); a spec
    whose previous pass is still running is skipped, never double-processed.
    Without one (tests, one-shot callers), everything runs inline as before.
    """
    # (status to walk, log label, processor, spec status to set on error).
    # Built per call, not at module level: _process_executing is defined
    # further down, and late binding is what lets tests stub the processors.
    passes = (
        # Clarification before planner: inline, an approved clarification
        # gate moves a spec to PENDING_PLAN in time for the planner pass of
        # the SAME tick. Scheduled, it lands next tick — one POLL_INTERVAL,
        # not a stall.
        (SpecStatus.NEEDS_CLARIFICATION, "clarification",
         _process_needs_clarification, None),
        (SpecStatus.PENDING_PLAN, "planner", _process_pending_plan,
         SpecStatus.FAILED),
        (SpecStatus.PLAN_REVIEW, "plan-review", _process_plan_review, None),
        (SpecStatus.EXECUTING, "execution", _process_executing, None),
    )
    if scheduler is not None:
        scheduler.reap()
    for status, label, processor, status_on_error in passes:
        for spec in db.list_specs(status=status):
            if scheduler is None:
                _run_spec_pass(db, spec.id, status, label, processor,
                               status_on_error)
            else:
                scheduler.submit(spec.id, label, _run_spec_pass,
                                 db, spec.id, status, label, processor,
                                 status_on_error)


def _run_spec_pass(db: Database, spec_id: str, status: SpecStatus,
                   label: str, processor, status_on_error) -> None:
    """One spec's processing for one tick, with the per-state error policy.

    Re-fetches the spec first: a pass can wait behind a full worker pool
    while a gate decision or cancellation moves the spec on, and processing
    that stale snapshot would resurrect the old state.
    """
    try:
        spec = db.get_spec(spec_id)
        if spec is None or spec.status != status:
            return
        processor(db, spec)
    except Exception:
        logger.exception("spec %s: error during %s pass", spec_id, label)
        if status_on_error is not None:
            db.update_spec_status(spec_id, status_on_error)


# ── Execution state machine (Phase 2) ───────────────────────────────────────

# Maps task role → which gate type to create after the agent finishes.
_ROLE_TO_GATE_TYPE = {
    "architect": GateType.DESIGN_APPROVAL,
    "implementer": GateType.CODE_REVIEW,
    "reviewer": GateType.RELEASE_APPROVAL,
}

# Worker roles in pipeline order. Used to reset downstream tasks when an earlier
# role is retried — e.g. a supervisor design-revision (retry→architect) must also
# re-run the implementer + reviewer, else the revised design is never built.
_ROLE_ORDER = {"architect": 0, "implementer": 1, "reviewer": 2}


def _process_executing(db: Database, spec: Spec) -> None:
    """Drive a spec through its task DAG while it's in EXECUTING state.

    On each tick we:
      1. Bootstrap tasks from the YAML if they don't exist yet.
      2. Find the first non-finished task.
      3. Either start it, check its review gate, or handle crash recovery.
    """
    tasks = db.list_tasks_for_spec(spec.id)
    # Bootstrap when there are no tasks (fresh spec) OR every task is SKIPPED
    # (post-replan: supervisor invalidated the prior task DAG and we re-entered
    # EXECUTING with a new plan). Without this, a replan that completes leaves
    # only DONE+SKIPPED tasks, _find_current_task returns None, and the spec
    # gets falsely marked DONE without ever re-running the new plan.
    if not tasks or all(t.status == TaskStatus.SKIPPED for t in tasks):
        _bootstrap_tasks(db, spec)
        return

    current = _find_current_task(tasks)
    if current is None:
        # All tasks done — mark spec done.
        logger.info("spec %s: all tasks completed, marking DONE", spec.id)
        db.update_spec_status(spec.id, SpecStatus.DONE)
        return

    if current.status == TaskStatus.PENDING:
        _start_task(db, spec, current)
    elif current.status == TaskStatus.RUNNING:
        # Shouldn't happen in normal operation since agent calls block the
        # tick. If we see RUNNING, the daemon crashed mid-call. Each reset
        # burns a retry (DEV-193): a task whose agent call deterministically
        # crashes the daemon otherwise loops RUNNING → crash → systemd
        # restart → PENDING forever — restarts more than 60s apart never
        # trip StartLimitBurst, so nothing else bounds the loop.
        if current.retry_count >= MAX_RETRIES:
            logger.error("spec %s: task %s crash-recovered %d times "
                         "(MAX_RETRIES=%d) — failing the spec instead of "
                         "looping", spec.id, current.id, current.retry_count,
                         MAX_RETRIES)
            db.update_task_status(current.id, TaskStatus.FAILED)
            db.update_spec_status(spec.id, SpecStatus.FAILED)
            return
        db.increment_task_retry(current.id)
        logger.warning("spec %s: task %s stuck in RUNNING (crash recovery?), "
                       "resetting to PENDING (recovery %d/%d)",
                       spec.id, current.id, current.retry_count + 1, MAX_RETRIES)
        db.update_task_status(current.id, TaskStatus.PENDING)
    elif current.status == TaskStatus.BLOCKED_ON_REVIEW:
        _check_execution_gate(db, spec, current)


def _load_plan(spec: Spec) -> dict:
    """Parse spec.normalized_yaml into a dict; {} when absent or malformed.

    Callers that merely want to *read* plan fields — implementer
    clarifications, reviewer test_strategy, synthesis framework opts — each
    used to re-implement this guard, and not all of them got it right. A plan
    that does not parse into a mapping already fails loudly in
    _bootstrap_tasks, before the spec can reach EXECUTING, so by the time
    these readers run the YAML is known good and {} means "nothing to read"
    rather than "silently wrong". Keep _bootstrap_tasks itself on raw
    safe_load: that is the one caller that must fail the spec.
    """
    import yaml as _yaml
    if not spec.normalized_yaml:
        return {}
    try:
        plan = _yaml.safe_load(spec.normalized_yaml)
    except _yaml.YAMLError as exc:
        logger.warning("spec %s: failed to parse plan.yaml: %s", spec.id, exc)
        return {}
    return plan if isinstance(plan, dict) else {}


def _bootstrap_tasks(db: Database, spec: Spec) -> None:
    """Parse the planner's YAML into Task rows."""
    import yaml as _yaml
    if not spec.normalized_yaml:
        # State machine reached EXECUTING without a plan (supervisor replan
        # path or hand-edited DB row). Without this guard, safe_load(None)
        # returns None and plan.get("phases") AttributeErrors with a stack
        # trace that's hard to read in the daemon log.
        logger.error("spec %s: EXECUTING with no normalized_yaml — marking failed",
                     spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    plan = _yaml.safe_load(spec.normalized_yaml)
    if not isinstance(plan, dict):
        logger.error("spec %s: normalized_yaml is not a dict (got %s) — marking failed",
                     spec.id, type(plan).__name__)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    phases = plan.get("phases", [])
    if not phases:
        logger.error("spec %s: plan YAML has no phases — marking failed",
                     spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    # Non-mapping phase entries — `phases: [design, implement, test]` is
    # plausible LLM output — used to AttributeError on phase.get() every
    # tick: exception caught and logged, no tasks created, spec never
    # failing and never progressing. Fail it once, loudly (DEV-140).
    bad = [p for p in phases if not isinstance(p, dict)]
    if bad:
        logger.error(
            "spec %s: plan phases must be mappings, got %s — marking failed",
            spec.id, ", ".join(type(p).__name__ for p in bad),
        )
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    for phase in phases:
        role = phase.get("role", "implementer")
        db.create_task(
            spec_id=spec.id,
            agent=executor.role_to_agent(role),
            role=role,
            title=phase.get("name", role),
            description=str(phase.get("success", "")),
            execution_target=phase.get("execution_target", "server"),
        )
    logger.info("spec %s: bootstrapped %d tasks from plan YAML",
                spec.id, len(phases))


def _find_current_task(tasks: list) -> "Task | None":
    """Return the first task that isn't DONE or SKIPPED, or None."""
    for t in tasks:
        if t.status not in (TaskStatus.DONE, TaskStatus.SKIPPED):
            return t
    return None


def _start_task(db: Database, spec: Spec, task) -> None:
    """Call the appropriate agent, parse the response, create artifacts
    and a review gate. This is synchronous — it blocks the tick thread
    for the entire duration of the inference call.
    """
    # Compare-and-set claim (DEV-142): a second poller (manual debug run
    # beside the systemd unit) racing this tick must lose here, not both
    # call the agent and double-create gates/Jira issues.
    if not db.claim_task(task.id):
        logger.warning("spec %s: task %s was claimed by another poller — skipping",
                       spec.id, task.id)
        return
    spec_dir = db.spec_dir(spec.id)

    try:
        if task.role == "architect":
            _run_architect(db, spec, task, spec_dir)
        elif task.role == "implementer":
            _run_implementer(db, spec, task, spec_dir)
        elif task.role == "reviewer":
            _run_reviewer(db, spec, task, spec_dir)
        else:
            logger.error("spec %s: unknown role %r for task %s",
                         spec.id, task.role, task.id)
            db.update_task_status(task.id, TaskStatus.FAILED)
    except ShutdownRequested as e:
        # SIGTERM mid-manifest (DEV-141): stop cleanly, keep the work
        # re-runnable. Deliberately does NOT burn a retry — shutting the
        # daemon down is an operator action, not a task failure.
        logger.info("spec %s: task %s (%s) interrupted by shutdown (%s) — "
                    "resetting to PENDING", spec.id, task.id, task.role, e)
        db.update_task_status(task.id, TaskStatus.PENDING)
    except _TRANSPORT_ERRORS as e:
        # Couldn't reach the server mid-inference (redeploy race / read timeout).
        # Mirror the RUNNING crash-recovery path: reset the task to PENDING and
        # leave the spec EXECUTING so the next tick re-runs it, instead of
        # failing an approved spec on a network hiccup.
        logger.warning("spec %s: task %s (%s) hit transport error (%s) — "
                       "resetting to PENDING for retry",
                       spec.id, task.id, task.role, type(e).__name__)
        db.update_task_status(task.id, TaskStatus.PENDING)
    except Exception:
        logger.exception("spec %s: task %s (%s) failed with exception",
                         spec.id, task.id, task.role)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)


def _run_architect(db: Database, spec: Spec, task, spec_dir) -> None:
    spec_md = (spec_dir / spec.source_md_path).read_text()
    # On a re-run (design-review rejection #3, or supervisor design-revision #4),
    # feed the failure back so the architect fixes the design instead of
    # regenerating the same document.
    rejection_notes = (_latest_architect_feedback(db, spec, spec_dir)
                       if task.retry_count > 0 else None)
    # The approved plan carries decisions (language, framework, dependency
    # policy, operator clarifications) that override any ambiguity left in
    # spec.md — without it the architect re-derives them from the spec alone
    # (DEV-107). File first, DB copy as fallback; a spec can reach EXECUTING
    # only through plan approval, so absence is worth a warning.
    plan_yaml: "str | None" = None
    try:
        plan_yaml = (spec_dir / "plan.yaml").read_text()
    except OSError:
        plan_yaml = spec.normalized_yaml
    if not plan_yaml:
        logger.warning("spec %s: no plan.yaml on disk or in DB — architect "
                       "runs without plan constraints", spec.id)
    plan_conditions = _approved_gate_conditions(
        db, spec.id, GateType.PLAN_APPROVAL)
    if plan_conditions:
        logger.info("spec %s: carrying %d chars of plan-approval conditions "
                    "into the architect prompt (DEV-546)",
                    spec.id, len(plan_conditions))
    messages = build_architect_message(
        spec_md, rejection_notes=rejection_notes, plan_yaml=plan_yaml,
        reference_files=_fetch_protected_files_for_spec(spec),
        approval_conditions=plan_conditions)

    # Architect output is structured (<<<DESIGN>>> / <<<COMPLEXITY>>> blocks).
    # The model occasionally drifts and returns prose without the markers; one
    # such miss used to fail the whole spec. We now retry the call up to
    # ARCHITECT_PARSE_RETRIES times before giving up. Each failed response is
    # persisted alongside spec.md so the post-mortem isn't blind.
    max_attempts = executor.ARCHITECT_PARSE_RETRIES + 1
    result = None
    for attempt in range(1, max_attempts + 1):
        meta: dict = {}
        raw = call_agent("architect", messages, meta=meta,
                         memory_query=executor.spec_memory_query(spec_md))
        _note_truncation(db, spec, task, "architect", meta, executor.ARCHITECT_MAX_TOKENS)
        result = parse_architect_response(raw)
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "architect",
                                 "result_kind": type(result).__name__,
                                 "attempt": attempt,
                                 **executor.agent_event_fields(meta)})
        if not isinstance(result, ParseError):
            if attempt > 1:
                logger.info("spec %s: architect parsed cleanly on attempt %d/%d",
                            spec.id, attempt, max_attempts)
            break

        # ParseError — persist the raw response and either retry or give up.
        try:
            (spec_dir / f"architect_failed_response_attempt{attempt}.txt").write_text(
                f"# parse error: {result.reason}\n\n{raw}"
            )
        except OSError as e:
            logger.warning("spec %s: could not persist failed architect "
                           "response (attempt %d): %s", spec.id, attempt, e)
        logger.warning(
            "spec %s: architect parse failed attempt %d/%d: %s",
            spec.id, attempt, max_attempts, result.reason,
        )

    if isinstance(result, ParseError):
        logger.error("spec %s: architect exhausted %d parse-retry attempt(s); "
                     "spec FAILED. Raw responses persisted as "
                     "architect_failed_response_attempt*.txt",
                     spec.id, max_attempts)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    # Write design.md
    _write_artifact(spec_dir, "design.md", result.design_md)
    db.create_artifact(spec_id=spec.id, task_id=task.id,
                       kind=ArtifactKind.DESIGN, path="design.md")

    # Persist complexity assessment as a workspace artifact. None when the
    # architect skipped or malformed the COMPLEXITY block — _run_implementer
    # falls back to the env-default agent in that case.
    if result.complexity:
        import json as _json
        _write_artifact(spec_dir, "complexity.json",
                        _json.dumps(result.complexity, indent=2) + "\n")
        logger.info("spec %s: architect complexity assessment: tier=%r agent=%r",
                    spec.id, result.complexity.get("tier"),
                    result.complexity.get("recommended_agent"))

    # Testability check (DEV-481 fix 1): before spending a design-review call or
    # a human's attention, verify mechanically that the design's own Criterion
    # Seams resolve against the API it specifies. This runs FIRST because it is
    # free, deterministic, and catches the class that has now killed two runs —
    # a criterion whose type is never declared Equatable. Bounded separately from
    # the design review so a testability bounce cannot consume the review's
    # single revision, and capped so it can never loop.
    if (executor.TESTABILITY_CHECK_ENABLED
            and task.retry_count < executor.TESTABILITY_CHECK_MAX_ROUNDS):
        try:
            # DEV-509 runs alongside DEV-481's check: same gate, same budget,
            # same feedback file. Completeness first — a design missing a type
            # declaration strands its seams too, and naming the root cause
            # beats reporting the symptom.
            findings = (
                design_testability.check_design_completeness(result.design_md)
                + design_testability.check_design_testability(result.design_md)
            )
        except Exception as exc:
            # Regex-heavy parsing of LLM prose. A crash here must not cost a
            # spec that has an otherwise usable design — the design review and
            # the human gate both still sit downstream.
            logger.warning("spec %s: testability check raised (%s) — "
                           "proceeding without it", spec.id, exc)
            findings = []
        if findings:
            kinds = sorted({f.kind for f in findings})
            logger.info("spec %s: testability check found %d finding(s) %s — "
                        "revising (architect retry %d)", spec.id, len(findings),
                        kinds, task.retry_count + 1)
            db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                            payload={"role": "testability_check",
                                     "model_call": False,
                                     "findings": len(findings), "kinds": kinds})
            try:
                (spec_dir / "design_review_feedback.md").write_text(
                    design_testability.format_findings(findings))
            except OSError as e:
                logger.warning("spec %s: could not persist testability "
                               "feedback: %s", spec.id, e)
            db.increment_task_retry(task.id)
            db.update_task_status(task.id, TaskStatus.PENDING)
            return

    # Design review (#3): critique the design BEFORE implementation, since the
    # implementer follows it exactly. Bounded by the architect retry budget and
    # fail-open (a flaky review never blocks). On a substantive FAIL, bounce back
    # to the architect with the notes instead of building a known-flawed design.
    if (executor.DESIGN_REVIEW_ENABLED
            and task.retry_count < executor.DESIGN_REVIEW_MAX_REVISIONS):
        verdict, notes = _run_design_review(db, spec, task, spec_dir,
                                            spec_md, result.design_md)
        if verdict == "FAIL" and notes:
            logger.info("spec %s: design review REJECTED the design — revising "
                        "(architect retry %d)", spec.id, task.retry_count + 1)
            try:
                (spec_dir / "design_review_feedback.md").write_text(notes)
            except OSError as e:
                logger.warning("spec %s: could not persist design-review feedback: %s",
                               spec.id, e)
            db.increment_task_retry(task.id)
            db.update_task_status(task.id, TaskStatus.PENDING)
            return

    # Create design_approval gate
    db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
    db.create_gate(
        spec_id=spec.id,
        task_id=task.id,
        gate_type=GateType.DESIGN_APPROVAL,
        prompt_md=(
            f"## Design ready for review: {spec.title}\n\n"
            f"Spec ID: `{spec.id}`\n\n"
            f"The architect has produced the following design. Approve to "
            f"begin implementation, or reject with notes.\n\n---\n\n"
            f"{result.design_md}\n"
        ),
    )
    logger.info("spec %s: architect done, design_approval gate created",
                spec.id)


def _approved_gate_conditions(db: Database, spec_id: str, gate_type) -> "str | None":
    """Notes a human attached when APPROVING a gate of *gate_type* (DEV-546).

    Rejection notes propagate and demonstrably work — three design rejections
    on run 9 each produced a design that addressed the named items. Approval
    notes were stored on the gate row, mirrored to Jira, and read by nobody,
    while the API accepted `notes` identically on both decisions and the gate
    prompt invited them. Nothing told the reviewer the difference.

    That is not merely a gap, it forces a false choice. The correct review of
    run 9's design 6 was "approve, and fix these three one-liners while you
    implement". The options on offer were "approve and say nothing that
    matters" or "reject and spend another architect round on three lines".
    Taking the former cost an implementer attempt rediscovering, through the
    compiler, a defect that was already written down.
    """
    gate = _latest_gate_of_type(db, spec_id, gate_type)
    if gate is None or gate.status != GateStatus.APPROVED:
        return None
    notes = (gate.reviewer_notes or "").strip()
    return notes or None


def _latest_architect_feedback(db: Database, spec: Spec, spec_dir: Path) -> "str | None":
    """Feedback for an architect re-run, combining the two sources:
      * design_review_feedback.md — the design-review rejection (#3), consumed
        once (deleted after reading so it doesn't bleed into a later cycle).
      * the supervisor's design-revision directive (#4), from the decision log.
    Returns the combined notes, or None when there's nothing to inject."""
    parts: list[str] = []
    fb_file = spec_dir / "design_review_feedback.md"
    if fb_file.is_file():
        try:
            parts.append(fb_file.read_text())
        except OSError:
            pass
        try:
            fb_file.unlink()
        except OSError:
            pass
    sup = _latest_supervisor_feedback(db, spec.id, target_role="architect")
    if sup:
        parts.append(sup)
    combined = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return combined or None


def _run_design_review(db: Database, spec: Spec, task, spec_dir,
                       spec_md: str, design_md: str) -> "tuple[str, str]":
    """LLM critique of the architect's design before implementation (#3).

    Returns (verdict, notes) with verdict in {"PASS","FAIL"}. FAIL-OPEN: any
    transport error, truncation, or unparseable verdict returns ("PASS", "") so
    a flaky review never blocks the pipeline. Uses the light/fast design-review
    agent (Coder-30B by default) for turnaround."""
    meta: dict = {}
    try:
        raw = call_agent(
            "reviewer",
            executor.build_design_review_message(spec_md, design_md),
            agent=executor.DESIGN_REVIEW_AGENT,
            max_tokens=executor.DESIGN_REVIEW_MAX_TOKENS,
            meta=meta,
        )
    except Exception as exc:  # transport/HTTP — never let it kill the spec
        logger.warning("spec %s: design review call failed (%s) — proceeding "
                       "without it", spec.id, exc)
        return "PASS", ""
    if meta.get("truncated"):
        logger.warning("spec %s: design review truncated (agent=%s) — proceeding "
                       "without it", spec.id, meta.get("agent"))
        return "PASS", ""
    verdict, notes = executor.parse_design_review(raw)
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "design_review", "verdict": verdict,
                             **executor.agent_event_fields(meta)})
    try:
        (spec_dir / "design_review.md").write_text(f"VERDICT: {verdict}\n\n{raw}")
    except OSError:
        pass
    logger.info("spec %s: design review verdict=%s", spec.id, verdict)
    return verdict, notes


# Matches `path/to/file.ext:LINE` — the citation format the reviewer prompt
# directs the LLM to use in `### Verdict Evidence`. Tolerates 0+ subdirs and
# any extension so we don't have to enumerate languages.
#
# The extension must contain at least one letter (`\w*[A-Za-z]\w*`) and the
# match cannot follow `://`, so `host.tld:port` / `IP:port` inside a URL is no
# longer mistaken for a `path/file.ext:line` citation. Observed false positive:
# `http://192.0.2.10:3001/` matched `50.101:3001` and got annotated
# mid-URL, logging a spurious "100% unverified" on clean reviews.
_CITE_RE: re.Pattern[str] = re.compile(
    r'(?<!://)\b((?:[\w\-]+/)*[\w\-]+\.\w*[A-Za-z]\w*):(\d+)\b'
)


def _verify_review_citations(review_md: str, spec_dir: Path) -> tuple[str, int, int]:
    """Annotate `path:line` citations in *review_md* that don't resolve to
    real files in *spec_dir*. Returns (annotated_md, n_checked, n_unverified).

    The reviewer LLM hallucinates ~1/3 of file-existence claims per
    project_implementer_rotation. Stripping the verdict outright is
    risky — sometimes the cite is a typo on a real bug — so we keep the
    LLM's verdict text and append `[unverified — file not in spec dir]`
    inline after each bogus cite. Human gate sees the discrediting
    annotations alongside the verdict; downstream synthesis can also
    weight unverified claims lower.

    Falls back to a no-op if any citation resolves outside spec_dir
    (path traversal attempts, absolute paths, etc.).
    """
    checked = 0
    unverified = 0
    spec_root = spec_dir.resolve()

    def _annotate(match: re.Match[str]) -> str:
        nonlocal checked, unverified
        path, _line = match.group(1), match.group(2)
        candidate = (spec_dir / path).resolve()
        # Refuse to validate paths that escape spec_dir (../etc/passwd-style).
        try:
            candidate.relative_to(spec_root)
        except ValueError:
            return match.group(0)
        checked += 1
        if candidate.is_file():
            return match.group(0)
        unverified += 1
        return f"{match.group(0)} [unverified — file not in spec dir]"

    annotated = _CITE_RE.sub(_annotate, review_md)
    return annotated, checked, unverified








# ── Implementation generation: single-call vs manifest/per-file (#4) ──────────

def _generate_implementation(
    db: Database, spec: Spec, task, spec_dir,
    spec_md: str, design_md: str, chosen_agent: "str | None",
    clarifications: list, rejection_notes: "str | None",
    tally: "dict | None" = None,
) -> "ImplementerResult | ParseError":
    """Produce the implementation for a spec.

    Large designs go file-by-file via a manifest (no single-call output
    ceiling); small designs use the legacy one-shot call. Both return an
    ImplementerResult (list of (path, content)) or a ParseError, which the
    caller handles identically (rotation retry on ParseError).
    """
    n_files = executor.estimate_design_file_count(design_md)
    # DEV-546: conditions the reviewer attached when APPROVING this design.
    # Fetched here so both generation paths get them from one place.
    approval_conditions = _approved_gate_conditions(
        db, spec.id, GateType.DESIGN_APPROVAL)
    if approval_conditions:
        logger.info("spec %s: carrying %d chars of design-approval conditions "
                    "into the implementer prompt (DEV-546)",
                    spec.id, len(approval_conditions))
    if executor.use_manifest_mode(design_md):
        logger.info("spec %s: manifest mode (design enumerates ~%d files >= "
                    "threshold %d)", spec.id, n_files, executor.MANIFEST_FILE_THRESHOLD)
        return _generate_via_manifest(
            db, spec, task, spec_dir, spec_md, design_md,
            chosen_agent, clarifications, rejection_notes, tally=tally,
            approval_conditions=approval_conditions,
        )

    # Single-call path: one response with every file, budget scaled to the design.
    existing_files = _fetch_existing_files_for_spec(spec, spec_md)
    messages = build_implementer_message(
        spec_md, design_md, rejection_notes=rejection_notes,
        clarifications=clarifications, existing_files=existing_files,
        reference_files=_fetch_protected_files_for_spec(spec),
        approval_conditions=approval_conditions,
    )
    impl_max_tokens = executor.implementer_max_tokens_for(design_md)
    logger.info("spec %s: single-call implementer budget=%d tokens (~%d files)",
                spec.id, impl_max_tokens, n_files)
    meta: dict = {}
    raw = call_agent("implementer", messages, agent=chosen_agent,
                     max_tokens=impl_max_tokens, meta=meta)
    _note_truncation(db, spec, task, "implementer", meta, impl_max_tokens)
    if tally is not None:
        executor.accumulate_agent_fields(tally, meta)
    return parse_implementer_response(raw)


def _persist_manifest(spec_dir, entries) -> None:
    """Write manifest.json so a later retry can reuse the file set and regenerate
    only the reviewer-cited files (targeted retry, #4b)."""
    try:
        data = [{"path": e.path, "purpose": e.purpose, "exports": e.exports}
                for e in entries]
        (spec_dir / "manifest.json").write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.warning("could not persist manifest.json: %s", exc)


def _load_prior_manifest_run(spec_dir, retry_count: int):
    """Load the previous attempt's manifest + file contents from its snapshot.

    Returns (entries, {path: content}) for a targeted retry, or None when no
    usable prior manifest is available (e.g. the prior attempt used single-call
    mode, or the snapshot is missing). The snapshot for attempt N-1 lives at
    ``retry_history/retry_<N-1>/`` (written by _clean_spec_dir_for_retry before
    the wipe)."""
    snap = spec_dir / "retry_history" / f"retry_{retry_count - 1}"
    mpath = snap / "manifest.json"
    if not mpath.is_file():
        return None
    try:
        data = json.loads(mpath.read_text())
        entries = [executor.ManifestEntry(path=d["path"], purpose=d.get("purpose", ""),
                                          exports=d.get("exports", ""))
                   for d in data]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("targeted retry: failed to load prior manifest.json: %s", exc)
        return None
    prior_files: dict = {}
    for e in entries:
        fp = snap / e.path
        if fp.is_file():
            try:
                prior_files[e.path] = fp.read_text()
            except OSError:
                pass
    if not entries or not prior_files:
        return None
    return entries, prior_files


# DEV-434: how many consecutive attempts may repeat the same diagnostics before
# the targeted retry gives up and regenerates everything. 1 means "the second
# identical failure widens".
TARGETED_RETRY_MAX_REPEATS = int(
    os.getenv("AUTONOMOUS_TARGETED_RETRY_MAX_REPEATS", "1"))

# Absolute worktree paths differ per dispatch (…/worktrees/spec_x-7f8a8795/…),
# and line numbers move as the file is rewritten. Neither changes what the
# defect IS, so both are stripped before comparing two failures.
_SIG_PATH_RE = re.compile(r"(/\S+?/)?([\w.+-]+\.\w+):\d+:\d+:")
_SIG_ERROR_RE = re.compile(r"error: (.+)")


def _attributed_diagnostics(notes: str) -> list:
    """Location-stripped message of every attributed diagnostic, in order.

    One entry per diagnostic *occurrence*. Callers asking "which defects are
    here?" build a set from this; callers asking "did the build get worse?"
    count it. Those are different questions, and the gap between them is wide:
    run 8's repair output carries 27 diagnostics drawn from 6 distinct
    messages, so deduplicating first discards most of the magnitude. A set
    comparison can therefore score a regression as an improvement whenever the
    new errors repeat one message — which is exactly what a dropped import
    does (DEV-541).

    Worktree paths and line numbers move between dispatches without changing
    what the defect is, so both are removed.
    """
    if not notes:
        return []
    msgs = []
    for line in notes.splitlines():
        # Only diagnostics that name a file:line say anything about the code.
        # Bare driver lines — `error: fatalError`, `error: emit-module command
        # failed…` — appear in essentially every failed build regardless of
        # cause: "fatalError" was present in 5 of 5 of spec_cc7dd609's build
        # failures. Counting them made every pair of consecutive failures look
        # like the same unfixable defect, which sent a perfectly good design
        # back for revision on no evidence at all.
        if not _ATTRIBUTED_ERROR_RE.search(line):
            continue
        match = _SIG_ERROR_RE.search(line)
        if not match:
            continue
        msg = _SIG_PATH_RE.sub(r"\2:", match.group(1).strip())
        if msg:
            msgs.append(msg)
    return msgs


def _diagnostic_messages(notes: str) -> set:
    """The distinct error messages in a failure report, location-stripped."""
    return set(_attributed_diagnostics(notes))


# ── Compiler warnings as signal (DEV-547) ────────────────────────────────────
#
# Run 9 of DEV-102 compiled, launched all 19 tests, and died on a runtime trap
# with zero tests completed. The defect was one inverted conditional that
# emptied `chains` on every step(), and the compiler had already named it, on
# the line, in output we captured and parsed:
#
#   World.swift:238:20: warning: value 'updateIdx' was defined but never used;
#                       consider replacing with boolean test [#no-usage]
#
# Errors drive control flow throughout this module; warnings were carried along
# as text and read by nobody. For model-written code that is the wrong trade.
# The warning classes a human reviewer learns to skim past are precisely the
# fingerprints of a model emitting confused control flow.
_BUILD_WARNING_RE = re.compile(
    r"^\s*(\S.*?):(\d+):(\d+): warning: (.+)$", re.MULTILINE)

# The trailing `[#no-usage]` id modern Swift appends. Absent on older
# toolchains and on most other compilers, so it is recorded when present and
# never required for a match.
_WARNING_DIAG_ID_RE = re.compile(r"\s*\[#([\w.-]+)\]\s*$")

# Deliberately narrow. A false positive costs a full implementer generation
# plus a runner dispatch, so this holds only classes where the compiler has
# *proved* that the code contradicts its apparent intent. Style warnings a
# human would rightly ignore — "never mutated; consider changing to 'let'",
# "was never used; consider replacing with '_'" on a loop index — are recorded
# and deliberately NOT blocked.
_BLOCKING_WARNING_RES = (
    # `if let x = <expr>` where x is never read. Swift emits this only when the
    # binding is pointless, which means the condition is not testing what it
    # appears to test. Run 9's defect, verbatim.
    re.compile(r"was defined but never used", re.I),
    # A branch the model wrote and then made unreachable.
    re.compile(r"will never be executed", re.I),
    # A condition the compiler can fold to a constant.
    re.compile(r"comparison .*?always (?:true|false)", re.I),
    re.compile(r"condition is always (?:true|false)", re.I),
)

# Kept switchable: this is the first check in the pipeline that can reject an
# attempt whose build *succeeded*, so it needs a way off without a deploy.
BLOCK_ON_BUILD_WARNINGS = os.getenv(
    "AUTONOMOUS_BLOCK_ON_BUILD_WARNINGS", "1").lower() not in ("0", "false", "no")


class BuildWarning(NamedTuple):
    """One `path:line:col: warning:` diagnostic lifted from a build."""
    path: str          # repo-relative where derivable, else as emitted
    line: int
    column: int
    diag_id: str       # "no-usage" etc., "" when the toolchain emits none
    message: str       # id stripped
    blocking: bool

    def located(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


def _short_diagnostic_path(path: str) -> str:
    """Drop the per-dispatch worktree prefix, keeping the repo-relative tail.

    Runner paths look like
    `/Users/youruser/…/worktrees/spec_9ff962b9-09f0ad65/Sources/CentipedeCore/World.swift`
    and the prefix changes on every dispatch, so it is noise in an artifact and
    breaks any comparison against `protected_paths`.
    """
    norm = (path or "").replace("\\", "/")
    marker = "/worktrees/"
    idx = norm.find(marker)
    if idx == -1:
        return norm
    tail = norm[idx + len(marker):]
    # …/worktrees/<dispatch-dir>/<repo-relative path>
    parts = tail.split("/", 1)
    return parts[1] if len(parts) == 2 else norm


def _parse_build_warnings(output: str,
                          protected_paths=None) -> "list[BuildWarning]":
    """Every located warning in *output*, flagged for whether it should block.

    A warning on a protected path is never blocking: the pipeline cannot edit
    those files, so rejecting an attempt over one would loop forever (DEV-427
    drops them before dispatch, so the worktree holds `main`'s copy).

    The caret echo line the compiler prints under a diagnostic repeats the
    message verbatim but carries no `path:line:col`, so it never matches and
    no de-duplication is needed for it.
    """
    if not output:
        return []
    protected = {str(p).strip().lstrip("./")
                 for p in (protected_paths or []) if p}
    seen = set()
    found: list[BuildWarning] = []
    for match in _BUILD_WARNING_RE.finditer(output):
        raw_path, line, column, message = match.groups()
        message = message.strip()
        diag_id = ""
        id_match = _WARNING_DIAG_ID_RE.search(message)
        if id_match:
            diag_id = id_match.group(1)
            message = _WARNING_DIAG_ID_RE.sub("", message).strip()
        path = _short_diagnostic_path(raw_path)
        key = (path, line, column, message)
        if key in seen:
            continue
        seen.add(key)
        on_protected = path in protected or any(
            path.endswith("/" + p) for p in protected)
        blocking = not on_protected and any(
            r.search(message) for r in _BLOCKING_WARNING_RES)
        found.append(BuildWarning(path, int(line), int(column),
                                  diag_id, message, blocking))
    return found


def _blocking_build_warnings(output: str,
                             protected_paths=None) -> "list[BuildWarning]":
    """The subset of _parse_build_warnings that should reject an attempt."""
    return [w for w in _parse_build_warnings(output, protected_paths)
            if w.blocking]


def _format_build_warnings(warnings: "list[BuildWarning]") -> str:
    """One `path:line:col: message [#id]` bullet per warning."""
    return "\n".join(
        f"  - {w.located()}: {w.message}"
        + (f"  [#{w.diag_id}]" if w.diag_id else "")
        for w in warnings)


def _build_warning_feedback(warnings: "list[BuildWarning]") -> str:
    """Implementer-facing note for a build that compiled but is not trustworthy.

    Careful not to repeat DEV-477's mistake in the other direction: this build
    *did* compile, and saying otherwise would send the implementer looking for
    a syntax error that does not exist.
    """
    listed = _format_build_warnings(warnings)
    return (
        "## The build succeeded, but the compiler contradicted the code\n\n"
        "No review was performed. The code compiled — this is not a build "
        "failure — but the compiler proved that the following lines do not do "
        "what they read as doing, and every one is in a file this spec "
        "generated:\n\n"
        f"{listed}\n\n"
        "These are not style notes. A conditional binding reported as unused "
        "means the condition is not testing what it appears to test; "
        "unreachable code and always-true comparisons mean a branch cannot "
        "run. Each one is a defect that compiles.\n\n"
        "Fix every warning above and re-emit ALL files.\n"
    )


def _failure_signature(notes: str) -> str:
    """Order- and location-independent fingerprint of a failure's diagnostics.

    Two attempts that produce the same set of error messages have the same
    signature even if the paths, line numbers and ordering differ.
    """
    return "|".join(sorted(_diagnostic_messages(notes)))


def _persistent_diagnostics(db, spec_id: str, current_notes: str,
                            *, lookback: int) -> set:
    """Diagnostics present in this failure AND each of the previous *lookback*.

    Whole-signature equality is too strict to detect an unfixable defect.
    Verified against spec_cc7dd609's five real build failures: no two
    consecutive attempts had identical error *sets*, because incidental errors
    came and went — yet `'mutating' is not valid on instance methods in
    classes` was present in four of the five, and `cannot assign to property:
    'type' is a 'let' constant` in four. Those are the design-caused ones, and
    they are exactly what survives a full regeneration.

    So the signal is an individual message that outlives repeated attempts,
    not a set that repeats verbatim.
    """
    current = _diagnostic_messages(current_notes)
    if not current or lookback < 1:
        return set()
    prior = [g for g in db.list_gates_for_spec(spec_id, GateType.CODE_REVIEW)
             if g.status is GateStatus.REJECTED and g.reviewer_notes]
    prior = list(reversed(prior))[:lookback]
    if len(prior) < lookback:
        return set()  # not enough history to call anything persistent
    for gate in prior:
        current &= _diagnostic_messages(gate.reviewer_notes)
        if not current:
            return set()
    return current


def _consecutive_identical_failures(db, spec_id: str, current_notes: str,
                                    *, already_recorded: bool = True) -> int:
    """How many prior consecutive attempts failed with the same diagnostics.

    `already_recorded` says whether *current_notes* has itself been written to
    a gate yet. On the retry path it has — the notes were read back off the
    latest rejected gate — so the newest match is this same failure and must
    not be counted. On the pre-gate build check it has not: the failure is in
    hand and nothing has recorded it, so every match is a genuine prior
    occurrence.

    Targeted retry regenerates only the files the compiler *cited*. When the
    fix lies outside those files — access control, a missing `@testable`, a
    signature mismatch — the cited files get rewritten forever and the defect
    never moves. Repetition is the signal that the selection is wrong, not the
    generation.
    """
    sig = _failure_signature(current_notes)
    if not sig:
        return 0
    gates = [g for g in db.list_gates_for_spec(spec_id, GateType.CODE_REVIEW)
             if g.status is GateStatus.REJECTED and g.reviewer_notes]
    newest_first = list(reversed(gates))
    # Skip at most ONE gate — the one carrying the notes we were handed.
    # Skipping every gate whose text matches would discard the repeats
    # themselves, since a repeated failure is byte-identical by definition.
    if (already_recorded and newest_first
            and newest_first[0].reviewer_notes == current_notes):
        newest_first = newest_first[1:]
    count = 0
    for gate in newest_first:
        if _failure_signature(gate.reviewer_notes) == sig:
            count += 1
        else:
            break
    return count


def _parse_cited_paths(rejection_notes: str, known_paths) -> set:
    """Manifest file paths the reviewer cited in its rejection notes.

    Matches a known path in full (``ParamountDemo/server/resolver.ts``) or by
    basename as a whole token (so ``resolver.ts:129`` resolves too)."""
    cited = set()
    for path in known_paths:
        if path in rejection_notes:
            cited.add(path)
            continue
        base = os.path.basename(path)
        if base and re.search(r"(?<![\w./-])" + re.escape(base) + r"(?![\w])",
                              rejection_notes):
            cited.add(path)
    return cited


def _build_from_manifest(
    db, spec, task, spec_md, design_md, entries, chosen_agent,
    clarifications, rejection_notes, *, prior_files, only, raw,
    tally: "dict | None" = None,
    approval_conditions: "str | None" = None,
) -> "ImplementerResult | ParseError":
    """Produce every file in the manifest, in dependency order.

    When *only* is a set of paths (targeted retry), files NOT in it are reused
    verbatim from *prior_files*; the cited files are regenerated with the
    reviewer feedback. When *only* is None, every file is generated."""
    written: list = []
    failures: list = []
    stale_fallbacks: list = []
    generated = 0
    # Fetched once for the whole manifest rather than per file: one runner call
    # instead of N, which matters on a link that drops every ~66s (DEV-518).
    existing_by_path = dict(_fetch_existing_files_for_spec(spec, spec_md))
    # Fetched once for the whole manifest, same as the editable files: each
    # per-file call is isolated and would otherwise be blind to what the
    # protected scaffold already declares (Centipede run 8).
    reference_files = _fetch_protected_files_for_spec(spec)
    for entry in entries:
        # Manifest mode chains one blocking agent call per file — the
        # longest uninterruptible stretch in the daemon. Check for a
        # pending SIGTERM between files so systemd stop doesn't escalate
        # to SIGKILL mid-manifest (DEV-141).
        if _shutdown_flag.set:
            raise ShutdownRequested(
                f"shutdown requested after {generated} of {len(entries)} files")
        if only is not None and entry.path not in only and prior_files \
                and entry.path in prior_files:
            content = prior_files[entry.path]  # reuse prior — not cited
        else:
            content = _generate_one_file(
                db, spec, task, spec_md, design_md, entries, entry,
                written, chosen_agent, clarifications, rejection_notes,
                existing_by_path=existing_by_path, tally=tally,
                reference_files=reference_files,
                approval_conditions=approval_conditions,
            )
            generated += 1
        if content is None and prior_files and entry.path in prior_files:
            # Regeneration of a cited file failed. Dropping it would leave a
            # manifest-declared file absent from the workspace, and every
            # test run after that fails on the missing module instead of a
            # code defect — unrecoverable (DEV-106). The prior attempt's
            # version is stale but keeps the file set complete; the reviewer
            # can re-cite it next round.
            content = prior_files[entry.path]
            stale_fallbacks.append(entry.path)
            logger.warning("spec %s: regeneration failed for cited file %s — "
                           "falling back to the prior attempt's version",
                           spec.id, entry.path)
        if content is None:
            failures.append(entry.path)
            logger.warning("spec %s: per-file generation failed for %s",
                           spec.id, entry.path)
            continue
        written.append((entry.path, content))

    if failures or stale_fallbacks:
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "manifest",
                                 "agent": chosen_agent or executor.role_to_agent("implementer"),
                                 "model_call": False,
                                 "anomaly": "per_file_failures",
                                 "paths": failures,
                                 "stale_fallbacks": stale_fallbacks})
    if not written:
        return ParseError(
            f"manifest mode produced no usable files ({len(failures)} failures)", raw)
    logger.info("spec %s: manifest build wrote %d/%d files "
                "(%d generated, %d reused, %d failed)", spec.id, len(written),
                len(entries), generated, len(written) - generated, len(failures))
    return ImplementerResult(files=written, raw=raw)


def _generate_via_manifest(
    db: Database, spec: Spec, task, spec_dir,
    spec_md: str, design_md: str, chosen_agent: "str | None",
    clarifications: list, rejection_notes: "str | None",
    tally: "dict | None" = None,
    approval_conditions: "str | None" = None,
) -> "ImplementerResult | ParseError":
    """Manifest → per-file generation.

    On a retry with reviewer feedback (#4b), reuse the prior attempt's manifest
    and regenerate ONLY the cited files (reusing the rest from the snapshot),
    skipping the manifest call and the untouched files. Otherwise do a full
    generation: one manifest call, then one bounded call per file."""
    retry_count = getattr(task, "retry_count", 0) or 0

    # ── Targeted retry: regenerate only the reviewer-cited files (#4b) ─────────
    if retry_count > 0 and rejection_notes:
        prior = _load_prior_manifest_run(spec_dir, retry_count)
        if prior is not None:
            entries, prior_files = prior
            # DEV-500: repair here too, not just on full generation. A targeted
            # retry reuses the PRIOR manifest, so a corrupted directory is
            # baked in and every regeneration rewrites the file to the same
            # unbuildable path — the retry cannot fix what the manifest keeps
            # asserting. Repair before computing the cited set so the citation
            # matches the corrected path.
            _repair_manifest_dirs(db, spec, task, entries, design_md)
            cited = _parse_cited_paths(rejection_notes, {e.path for e in entries})
            widened = False

            # DEV-434: the cited files are the ones the compiler NAMED, which
            # is not always where the fix belongs. An access-control error, a
            # missing @testable, a signature mismatch — all of them name the
            # victim rather than the cause, so regenerating the cited set
            # rewrites the same files forever while the defect sits elsewhere.
            # Repetition is the tell, so widen on it rather than looping.
            repeats = _consecutive_identical_failures(db, spec.id, rejection_notes)
            if cited and repeats >= TARGETED_RETRY_MAX_REPEATS:
                logger.warning(
                    "spec %s: same diagnostics as the previous %d attempt(s) — "
                    "the fix is not in the cited files; regenerating all %d "
                    "instead of %d", spec.id, repeats, len(entries), len(cited))
                db.record_event(
                    EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "manifest", "mode": "widened_after_repeat",
                             "model_call": False,
                             "repeats": repeats, "would_have_regenerated": sorted(cited),
                             "regenerating": len(entries)})
                cited, widened = set(), True  # full regeneration below

            # DEV-435: a diagnostic with no file:line cannot be attributed, so
            # the cited set is whatever cascade errors happened to carry a
            # location — usually the victims. Do not trust it.
            elif cited and _unattributed_errors(rejection_notes):
                logger.warning(
                    "spec %s: build reported an error with no file:line; the "
                    "cited files may be consequences — regenerating all %d "
                    "instead of %d", spec.id, len(entries), len(cited))
                db.record_event(
                    EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "manifest", "mode": "widened_unattributed",
                             "model_call": False,
                             "would_have_regenerated": sorted(cited),
                             "regenerating": len(entries)})
                cited, widened = set(), True

            if cited:
                logger.info("spec %s: targeted retry — regenerating %d/%d cited "
                            "files: %s", spec.id, len(cited), len(entries),
                            ", ".join(sorted(cited)))
                db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                                payload={"role": "manifest", "mode": "targeted_retry",
                                         "agent": chosen_agent or executor.role_to_agent("implementer"),
                                         "model_call": False,
                                         "regenerated": sorted(cited),
                                         "reused": len(entries) - len(cited)})
                result = _build_from_manifest(
                    db, spec, task, spec_md, design_md, entries, chosen_agent,
                    clarifications, rejection_notes,
                    prior_files=prior_files, only=cited, raw="targeted-retry",
                    tally=tally, approval_conditions=approval_conditions,
                )
                if not isinstance(result, ParseError):
                    _persist_manifest(spec_dir, entries)
                return result
            elif not widened:
                logger.info("spec %s: targeted retry wanted but no cited files "
                            "matched the manifest — full regeneration", spec.id)

    # ── Full generation: manifest call + per-file ─────────────────────────────
    meta: dict = {}
    manifest_raw = call_agent(
        "implementer", build_manifest_message(spec_md, design_md, clarifications,
                                             approval_conditions=approval_conditions),
        agent=chosen_agent, max_tokens=executor.MANIFEST_MAX_TOKENS, meta=meta,
    )
    _note_truncation(db, spec, task, "manifest", meta, executor.MANIFEST_MAX_TOKENS)
    # Before the ParseError return: a manifest call that failed to parse still
    # cost the attempt a full generation, and rotating away from it is exactly
    # the case a cost-per-attempt query wants to see.
    if tally is not None:
        executor.accumulate_agent_fields(tally, meta)
    manifest = parse_manifest_response(manifest_raw)
    if isinstance(manifest, ParseError):
        logger.warning("spec %s: manifest parse failed (%s) — rotating",
                       spec.id, manifest.reason)
        return manifest  # propagate → caller's rotation retry
    manifest.entries = _drop_undeliverable_manifest_entries(spec, manifest.entries)
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "manifest",
                             "files": len(manifest.entries),
                             "paths": [e.path for e in manifest.entries],
                             **executor.agent_event_fields(meta)})
    logger.info("spec %s: manifest = %d files: %s", spec.id,
                len(manifest.entries), ", ".join(e.path for e in manifest.entries))

    _repair_manifest_dirs(db, spec, task, manifest.entries, design_md)

    result = _build_from_manifest(
        db, spec, task, spec_md, design_md, manifest.entries, chosen_agent,
        clarifications, rejection_notes,
        prior_files=None, only=None, raw=manifest.raw, tally=tally,
        approval_conditions=approval_conditions,
    )
    if not isinstance(result, ParseError):
        _persist_manifest(spec_dir, manifest.entries)
    return result


# Directory names as the design writes them. It uses a tree layout — `Tests/`
# on one line, `└── CentipedeCoreTests/` on the next — so a full path prefix
# never appears contiguously and matching on prefixes finds nothing. Match on
# individual components instead, which is layout-agnostic.
_DESIGN_DIR_COMPONENT_RE = re.compile(r"([A-Za-z][\w.-]*)/")


def _design_dir_components(design_md: str) -> set:
    """Directory names the design mentions, e.g. {'Sources', 'CentipedeCore',
    'Tests', 'CentipedeCoreTests'} — however the document lays them out."""
    return set(_DESIGN_DIR_COMPONENT_RE.findall(design_md))


def _closest_component(candidate: str, known: set) -> "str | None":
    """The known directory name one substitution from `candidate`, if exactly
    one is.

    Deliberately strict: same length, and either one substitution or one
    adjacent transposition. Both shapes were observed on the same run —
    `CentipedeCoreTests` → `CentipegeCoreTests` (substitution) and
    `CentipedeCore` → `CentidepeCore` (transposition, which is two
    substitutions and so needs its own case). Neither can collide with a
    genuinely new directory, which differs by whole words rather than by one
    letter. Requiring a unique match leaves an ambiguous case alone rather
    than guessing.
    """
    def _near(k: str) -> bool:
        if k == candidate or len(k) != len(candidate):
            return False
        diff = [i for i, (a, b) in enumerate(zip(k, candidate)) if a != b]
        if len(diff) == 1:
            return True
        if len(diff) == 2:
            # A swap of two characters. Not necessarily adjacent: the observed
            # `Centipede` → `Centidepe` swaps the p and d two apart.
            i, j = diff
            return k[i] == candidate[j] and k[j] == candidate[i]
        return False

    matches = [k for k in known if _near(k)]
    return matches[0] if len(matches) == 1 else None


def _repair_manifest_dirs(db, spec, task, entries, design_md: str) -> int:
    """Correct manifest paths whose directory is a near-miss typo (DEV-500).

    A one-character corruption in a directory name is silent and expensive: the
    file lands where no build target claims it, so it is never compiled. The
    sources still build, the pre-existing suite still passes, and the pre-gate
    check reports "compiled and the suite passed" for a spec that delivered
    nothing. Observed live on spec_9872c963, where the implementer emitted
    `Tests/CentipegeCoreTests/CoreLogicTests.swift` after writing the correct
    `Tests/CentipedeCoreTests/` on both previous attempts.

    Repairs in place and returns the number of paths corrected. A path whose
    directory the design never mentions is left alone — the design is the
    authority on layout, and inventing a correction would be worse than the
    typo.
    """
    known = _design_dir_components(design_md)
    if not known:
        return 0
    repaired = 0
    for e in entries:
        parts = e.path.split("/")
        if len(parts) < 2:
            continue
        fixed_parts, changed = [], False
        for part in parts[:-1]:            # directories only; never the filename
            if part in known:
                fixed_parts.append(part)
                continue
            near = _closest_component(part, known)
            if near is None:
                fixed_parts.append(part)
                continue
            fixed_parts.append(near)
            changed = True
        if not changed:
            continue
        corrected = "/".join(fixed_parts + [parts[-1]])
        logger.warning(
            "spec %s: manifest path %r has a directory one character from the "
            "design's — correcting to %r. Left alone, the file lands in no "
            "build target and the suite passes having tested nothing "
            "(DEV-500).", spec.id, e.path, corrected)
        e.path = corrected
        repaired += 1
    if repaired:
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "manifest", "model_call": False,
                                 "repaired_paths": repaired})
    return repaired


def _verify_manifest_workspace(db, spec, task, spec_dir) -> "tuple[list, list]":
    """Assert every manifest.json-declared path exists in the live workspace.

    A targeted retry regenerates a subset and reuses the rest, and one
    dropped file (per-file failure, snapshot gap) leaves the workspace
    permanently missing a test-required module: node --test then fails with
    ERR_MODULE_NOT_FOUND on every retry and the spec can never converge
    (DEV-106, spec_54b2c1b3). Missing files are materialized from the newest
    retry_history snapshot that has them; whatever cannot be restored is
    surfaced loudly (error log + event + gate prompt) instead of proceeding
    silently into a doomed test run.

    Returns ``(restored, still_missing)`` path lists. No-op (empty lists)
    when the attempt didn't use manifest mode — manifest.json is wiped by
    _clean_spec_dir_for_retry and only re-persisted on a manifest build.
    """
    mpath = spec_dir / "manifest.json"
    if not mpath.is_file():
        return [], []
    try:
        declared = [d["path"] for d in json.loads(mpath.read_text())]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("spec %s: unreadable manifest.json, skipping workspace "
                       "verification: %s", spec.id, exc)
        return [], []
    missing = [p for p in declared if not (spec_dir / p).is_file()]
    if not missing:
        return [], []

    def _snap_index(p):
        try:
            return int(p.name.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            return -1

    hist = spec_dir / "retry_history"
    snaps = sorted(hist.glob("retry_*"), key=_snap_index, reverse=True) \
        if hist.is_dir() else []
    restored, still_missing = [], []
    for rel in missing:
        content = None
        for snap in snaps:
            fp = snap / rel
            if fp.is_file():
                try:
                    content = fp.read_text()
                    break
                except OSError:
                    continue
        if content is None:
            still_missing.append(rel)
            continue
        _write_artifact(spec_dir, rel, content)
        db.create_artifact(spec_id=spec.id, task_id=task.id,
                           kind=ArtifactKind.CODE, path=rel)
        restored.append(rel)
    if restored:
        logger.warning("spec %s: restored %d manifest-declared file(s) missing "
                       "from the workspace: %s", spec.id, len(restored),
                       ", ".join(restored))
    if still_missing:
        logger.error("spec %s: manifest-declared file(s) MISSING from the "
                     "workspace and unrecoverable from snapshots: %s",
                     spec.id, ", ".join(still_missing))
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer",
                             "model_call": False,
                             "anomaly": "manifest_files_missing",
                             "restored": restored,
                             "still_missing": still_missing,
                             "retry": task.retry_count})
    return restored, still_missing


def _generate_one_file(
    db: Database, spec: Spec, task, spec_md: str, design_md: str,
    manifest_entries: list, entry, written: list, chosen_agent: "str | None",
    clarifications: list, rejection_notes: "str | None",
    existing_by_path: "dict[str, str] | None" = None,
    tally: "dict | None" = None,
    reference_files: "list[tuple[str, str]] | None" = None,
    approval_conditions: "str | None" = None,
) -> "str | None":
    """Generate a single file's content, with bounded parse-retries. Returns the
    content (associated with the manifest's canonical path) or None on failure."""
    written_summary = summarize_written_files(written)
    existing_content = (existing_by_path or {}).get(entry.path)
    for attempt in range(executor.PER_FILE_PARSE_RETRIES + 1):
        meta: dict = {}
        raw = call_agent(
            "implementer",
            build_per_file_message(spec_md, design_md, manifest_entries, entry,
                                   written_summary, clarifications, rejection_notes,
                                   existing_content=existing_content,
                                   reference_files=reference_files,
                                   approval_conditions=approval_conditions),
            agent=chosen_agent, max_tokens=executor.PER_FILE_MAX_TOKENS, meta=meta,
            memory_query=executor.file_memory_query(entry),
        )
        # Counted before any early return below: a truncated or unparseable
        # call still spent the GPU time and the tokens, and an attempt's cost
        # that omits its failed calls understates exactly the attempts worth
        # studying.
        if tally is not None:
            executor.accumulate_agent_fields(tally, meta)
        _note_truncation(db, spec, task, f"per-file:{entry.path}", meta,
                         executor.PER_FILE_MAX_TOKENS)
        if meta.get("truncated"):
            # Degenerate generation: the model burned the whole budget without
            # finishing the file. Retrying the SAME model just truncates again
            # ~5.5 min later, so don't spend the remaining parse-retries on it —
            # bail now and let the caller rotate to the next implementer.
            logger.warning("spec %s: per-file:%s truncated (agent=%s) — skipping "
                           "remaining parse-retries to advance rotation",
                           spec.id, entry.path, meta.get("agent"))
            return None
        parsed = parse_implementer_response(raw)
        if isinstance(parsed, ParseError) or not parsed.files:
            continue
        # Enforce the manifest path: take the block whose path matches (by full
        # path or basename), else the first block the model emitted.
        target_base = os.path.basename(entry.path)
        for p, c in parsed.files:
            pp = p.strip().lstrip("/")
            if pp == entry.path or os.path.basename(pp) == target_base:
                return c
        return parsed.files[0][1]
    return None


# How many consecutive requeues to allow before admitting the runner is not
# coming back on its own. Each requeue costs one implementer generation, so
# this is not free: three covers a sleeping Mac or a link re-enumeration
# (DEV-518, which clears in seconds to minutes) without regenerating all night
# against a Mac that is simply switched off.
_MAX_UNREACHABLE_REQUEUES = 3


def _requeue_for_unreachable_runner(db: Database, spec: Spec, task) -> bool:
    """Put the task back in the queue after a transport-only build check.

    Returns True when the caller should return without opening a gate.

    Mirrors what the daemon already does when the *model* server is
    unreachable (`_TRANSPORT_ERRORS` in _run_task): reset to PENDING, leave the
    spec EXECUTING, let the next tick re-run it — and deliberately do not touch
    retry_count, because a sleeping Mac is not an implementer's mistake.

    Bounded, because a requeue re-runs the implementer and that is a whole
    generation. Past the cap we do open a gate, but one that says the runner is
    unreachable rather than one that asks someone to review code nobody has
    compiled.
    """
    prior = sum(
        1 for e in db.list_events_by_kind(
            spec_id=spec.id, kind=EventKind.TEST_RAN, limit=20)
        if (e.payload or {}).get("phase") == "pre_gate_build_check"
        and (e.payload or {}).get("runner_unreachable")
    )
    if prior >= _MAX_UNREACHABLE_REQUEUES:
        logger.error(
            "spec %s: runner still unreachable after %d requeue(s) — "
            "escalating to a human; this is an infrastructure fault, not a "
            "code review", spec.id, prior)
        return False

    db.record_event(EventKind.TEST_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"phase": "pre_gate_build_check",
                             "passed": False,
                             "runner_unreachable": True,
                             "requeue": prior + 1,
                             "retry": task.retry_count})
    db.update_task_status(task.id, TaskStatus.PENDING)
    logger.warning(
        "spec %s: mac-runner unreachable — requeued for retry %d/%d without "
        "burning an implementer attempt (still at %d/%d); the next tick will "
        "try again",
        spec.id, prior + 1, _MAX_UNREACHABLE_REQUEUES,
        task.retry_count, MAX_RETRIES)
    return True


def _drop_protected_type_collisions(db: Database, spec: Spec, task, files,
                                    role: str, protected_files):
    """Discard generated files that redeclare a type a protected file owns.

    DEV-552. Prompting is not enough on its own: run 10's repair invented
    `Field.swift` declaring a type the protected `GameState.swift` already
    declared, and every diagnostic in that final build came from that one file.
    Worse, no later attempt could have recovered — the file it collides with is
    one the pipeline is forbidden to edit, so the error is unreachable from
    anything the model is allowed to change.

    Conservative on purpose. A file is dropped only when EVERY top-level type
    it declares collides, which is the pure-duplicate case and loses nothing.
    A file that also declares wanted types is left alone and merely logged:
    the build will fail either way, and deleting real work to avoid a
    diagnostic would be the worse trade.
    """
    if not protected_files:
        return files
    try:
        collisions = executor.protected_type_collisions(files, protected_files)
    except Exception as e:  # never let a lint step break a generation
        logger.warning("spec %s: protected-type check errored (%s) — skipping",
                       spec.id, e)
        return files
    if not collisions:
        return files

    dropped = {path for path, _, total in collisions if total}
    for path, names, total in collisions:
        logger.warning(
            "spec %s: generated %s redeclares protected type(s) %s — %s",
            spec.id, path, ", ".join(names),
            "dropping the file" if total else
            "KEEPING it (it declares other types too); the build will fail")
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": role, "model_call": False,
                             "anomaly": "protected_type_collision",
                             "dropped": sorted(dropped),
                             "collisions": [
                                 {"path": p, "types": n, "dropped": t}
                                 for p, n, t in collisions]})
    return [(p, c) for p, c in files if p not in dropped]


def _normalize_generated_files(db: Database, spec: Spec, task, files, role: str,
                               protected_files=None):
    """Apply deterministic boilerplate fixes and record what changed.

    Every path that writes model-generated files must go through this. DEV-540
    was reachable precisely because only the implementer did: the synthesis and
    repair outputs were written raw, and the repair is where run 8 actually
    died — it emitted eight Swift files with no `import Foundation` and nothing
    stood between that and the compiler.

    DEV-552 hangs the protected-type check here for the same reason: this is
    the one choke point every generated file already passes through.
    """
    files = _drop_protected_type_collisions(
        db, spec, task, files, role, protected_files)
    normalized, notes = executor.normalize_boilerplate(files)
    if not notes:
        return files
    for note in notes:
        logger.info("spec %s: boilerplate normalized — %s", spec.id, note)
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": role, "model_call": False,
                             "normalized": notes})
    return normalized


def _run_implementer(db: Database, spec: Spec, task, spec_dir) -> None:
    # Wipe artifacts from earlier retries so the new implementer starts
    # from a clean slate. No-op on retry-0.
    _clean_spec_dir_for_retry(spec_dir, task.retry_count)

    spec_md = (spec_dir / spec.source_md_path).read_text()
    design_path = spec_dir / "design.md"
    design_md = design_path.read_text() if design_path.exists() else ""

    # Pull operator clarifications from plan.yaml so we can prepend them to
    # the implementer's user message. Belt-and-braces with the planner-side
    # `clarifications:` YAML embedding: even if the planner is sloppy or the
    # implementer skips that section of the plan, the orchestrator-supplied
    # list still lands at the top of the prompt with hard-requirement framing.
    raw_clar = _load_plan(spec).get("clarifications")
    clarifications: list[str] = (
        [str(c) for c in raw_clar if c] if isinstance(raw_clar, list) else []
    )

    # On retry, include rejection notes from the most recent code_review gate.
    rejection_notes = None
    if task.retry_count > 0:
        prev_gates = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)
        for g in reversed(prev_gates):
            if g.status == GateStatus.REJECTED and g.reviewer_notes:
                rejection_notes = g.reviewer_notes
                break

    # Pick the implementer. Retry 0 honors the architect's complexity-based
    # recommendation; later retries walk the rotation chain so each attempt
    # uses a different model family — see project_implementer_rotation.md.
    # Falling back to task.agent (env-default) when complexity.json is absent
    # so retries still rotate from a sensible anchor.
    initial_agent = _select_implementer_agent(spec_dir) or task.agent
    chosen_agent = _rotation_pick(initial_agent, task.retry_count)
    if chosen_agent and chosen_agent != task.agent:
        if task.retry_count == 0:
            logger.info("spec %s: architect recommendation overrides implementer agent: %r → %r",
                        spec.id, task.agent, chosen_agent)
        else:
            logger.info("spec %s: rotation pick (retry=%d): %r → %r",
                        spec.id, task.retry_count, task.agent, chosen_agent)
        db.update_task_agent(task.id, chosen_agent)

    # Generate the implementation — either as one call (small designs) or
    # file-by-file via a manifest (large designs). Both return an
    # ImplementerResult (list of files) or a ParseError handled identically below.
    # One attempt spans 1 call (single-call mode) or 1 + N (manifest mode);
    # the tally sums them so this event answers "what did this attempt cost"
    # with a single number per axis (DEV-528).
    tally: dict = {}
    result = _generate_implementation(
        db, spec, task, spec_dir, spec_md, design_md,
        chosen_agent, clarifications, rejection_notes, tally=tally,
    )

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer",
                             "result_kind": type(result).__name__,
                             "retry": task.retry_count,
                             # The tally reports the agent the calls actually
                             # went to; fall back to the rotation pick when no
                             # call reported one (every call raised).
                             "agent": chosen_agent or task.agent,
                             **executor.agent_event_fields(tally)})

    if isinstance(result, ParseError):
        logger.error("spec %s: implementer response unparseable: %s",
                     spec.id, result.reason)
        if task.retry_count >= MAX_RETRIES:
            logger.error(
                "spec %s: parse-failure retry budget exhausted (%d/%d), failing",
                spec.id, task.retry_count, MAX_RETRIES,
            )
            db.update_task_status(task.id, TaskStatus.FAILED)
            db.update_spec_status(spec.id, SpecStatus.FAILED)
            return
        # Engage rotation just like `_legacy_attempt_retry` does on test
        # failure: create a synthetic rejected code_review gate so the next
        # tick re-runs `_run_implementer` with a different model from the
        # rotation chain. Without this, a single unparseable response from
        # one agent (e.g. native_implementer wrapping its output in markdown headings
        # instead of <<<FILE:>>> markers) immediately fails the spec — even
        # though moe_implementer / fast_implementer / etc. would happily
        # produce the right output. Observed in spec_51b1baee retry-2 on
        # 2026-05-02; the orchestrator now rotates instead of giving up.
        synth_gate = db.create_gate(
            spec_id=spec.id,
            task_id=task.id,
            gate_type=GateType.CODE_REVIEW,
            prompt_md="## Automated parse-failure retry",
        )
        feedback = (
            f"Previous implementer response was unparseable: {result.reason}. "
            f"Re-emit ALL files. Each one must be wrapped in a complete "
            f"<<<FILE: path>>> ... <<<END_FILE>>> block — exactly three "
            f"angle brackets on every opening and closing marker. Do not "
            f"use markdown code fences, headings, or any other format for "
            f"file contents; the daemon parses ONLY the marker-delimited "
            f"blocks."
        )
        db.respond_to_gate(synth_gate.id, "rejected", notes=feedback)
        db.increment_task_retry(task.id)
        db.update_task_status(task.id, TaskStatus.PENDING)
        logger.info(
            "spec %s: implementer parse failed, rotating implementer "
            "(attempt %d/%d)",
            spec.id, task.retry_count + 1, MAX_RETRIES,
        )
        return

    # Surface duplicate-path collisions as a diagnostic event so the
    # dashboard timeline (and any "why does this file have unexpected
    # content" investigation) can see that the model emitted the same
    # path twice. The parser already deduped via last-write-wins.
    if result.duplicate_paths:
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer",
                                 "model_call": False,
                                 "anomaly": "duplicate_file_paths",
                                 "paths": result.duplicate_paths,
                                 "retry": task.retry_count})

    # Deterministically fix boilerplate the reviewer checks — unpinned deps,
    # and a Swift file missing `import Foundation` — so the most mechanical
    # FAILs never depend on the model getting them right. See
    # executor.normalize_boilerplate (#3, DEV-540).
    result.files = _normalize_generated_files(
        db, spec, task, result.files, "implementer",
        protected_files=_fetch_protected_files_for_spec(spec))

    # Write all files
    for rel_path, content in result.files:
        _write_artifact(spec_dir, rel_path, content)
        db.create_artifact(spec_id=spec.id, task_id=task.id,
                           kind=ArtifactKind.CODE, path=rel_path)

    # Every manifest-declared file must actually be in the workspace now — a
    # dropped one dooms the test loop to ERR_MODULE_NOT_FOUND forever
    # (DEV-106). Restore what the snapshots have; flag the rest loudly.
    restored, still_missing = _verify_manifest_workspace(db, spec, task, spec_dir)

    # DEV-429: build the code before asking a human to review it. A gate that
    # opens on code which cannot compile spends the expensive resource (the
    # reviewer) on something the free one (the compiler) already decided. The
    # reviewer/test task that would have caught it sits PENDING *behind* this
    # gate, so without this the ordering is inverted.
    #
    # A dispatch that errors out (Mac asleep, key missing, network) must not
    # burn a retry: _detect_build_failure only fires on a recognised compiler
    # diagnostic, and everything else falls through to the normal gate.
    build_reason = None
    build_output = ""
    build_passed = None
    build_framework = ""
    build_warnings: list = []
    blocking_warnings: list = []
    ts_for_build = _load_plan(spec).get("test_strategy")
    if isinstance(ts_for_build, dict) and ts_for_build.get("framework"):
        fw = build_framework = ts_for_build["framework"]
        try:
            build_passed, build_output = _run_tests_with_guard(
                spec.id, spec_dir, fw, ts_for_build,
                output_label="Pre-gate build check output:",
                fail_log=("spec %s: pre-gate build check failed structural "
                          "validation (%s)"),
            )
            build_reason = _detect_build_failure(build_output, fw, build_passed)
            # DEV-547: warnings are only consulted when nothing failed to
            # compile. A real diagnostic is strictly better feedback, and
            # stacking the two would bury it.
            build_warnings = _parse_build_warnings(
                build_output, ts_for_build.get("protected_paths"))
            if build_reason is None and BLOCK_ON_BUILD_WARNINGS:
                blocking_warnings = [w for w in build_warnings if w.blocking]
        except Exception as e:  # never let the check itself stall the spec
            logger.warning("spec %s: pre-gate build check errored (%s) — "
                           "falling through to the review gate", spec.id, e)
            build_reason = None
            blocking_warnings = []

        db.record_event(EventKind.TEST_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"phase": "pre_gate_build_check",
                                 "passed": build_passed if not build_reason else False,
                                 "build_failed": build_reason is not None,
                                 # DEV-547/DEV-529: warnings become queryable
                                 # rather than living only in the raw log.
                                 "warnings": len(build_warnings),
                                 "blocking_warnings": [
                                     {"path": w.path, "line": w.line,
                                      "diag_id": w.diag_id,
                                      "message": w.message}
                                     for w in blocking_warnings],
                                 # DEV-548: "compiled then crashed" is its own
                                 # outcome and DEV-529's taxonomy will want it
                                 # separated from a build failure.
                                 "test_process_crashed":
                                     _detect_test_process_crash(build_output),
                                 "retry": task.retry_count})

        # DEV-478: keep the runner's own words whatever the outcome. Previously
        # this was written only on the diagnostic path below, so the one case
        # where the reviewer most needs it — the check ran, something failed,
        # and it was not a recognised compiler diagnostic — kept nothing. The
        # reviewer's own test_output.txt is written by _run_reviewer_tests,
        # which sits *behind* this gate and has not run.
        if build_output:
            _write_artifact(spec_dir, "build_check_output.txt", build_output)
            db.create_artifact(spec_id=spec.id, task_id=task.id,
                               kind=ArtifactKind.TEST_REPORT,
                               path="build_check_output.txt")

        # DEV-477: neither a summary nor a diagnostic means the check told us
        # nothing — an infrastructure fault, not a verdict on the code.
        #
        # DEV-548 carves out the case where it told us plenty: a build that
        # completed and then took a signal. That is a runtime defect with a
        # clean compile behind it, so it is neither inconclusive nor an
        # infrastructure fault, and it must not be requeued as one.
        if (build_passed is False and build_reason is None
                and _detect_test_process_crash(build_output)):
            logger.warning(
                "spec %s: pre-gate build check compiled and then crashed — %s; "
                "this is a runtime defect, not a build failure",
                spec.id, _detect_test_process_crash(build_output))
        elif (build_passed is False and build_reason is None
                and not _observed_a_test_run(build_output, fw)):
            logger.warning(
                "spec %s: pre-gate build check is inconclusive — no test "
                "summary and no compiler diagnostic in %d chars of output; "
                "the build is unverified, not confirmed",
                spec.id, len(build_output))
            # DEV-538: and now act on it. Opening a code_review gate here asks
            # the most expensive, slowest resource in the system to adjudicate
            # a question no one has the evidence to answer, and then waits
            # forever — run 8 sat on exactly this for 3930 minutes across three
            # reboots. The runner comes back on its own, so the right move is
            # to put the work down and pick it up again.
            if test_runner.is_runner_unreachable(build_output):
                if _requeue_for_unreachable_runner(db, spec, task):
                    return

    if build_reason is not None or blocking_warnings:
        # Straight back to the implementer with the compiler's own words. No
        # human gate: there is nothing here for a reviewer to decide.
        if build_reason is not None:
            _write_artifact(spec_dir, "build_failure.txt", build_output)
            actionable = _extract_actionable_test_output(
                build_output, ts_for_build["framework"])
            feedback = (
                f"## The code does not compile\n\n"
                f"No review was performed — the build failed, so there is nothing "
                f"to review yet. First compiler diagnostic:\n\n"
                f"    {build_reason}\n\n"
                f"{_diagnostic_completeness_note(build_output)}"
                f"Fix every diagnostic below and re-emit ALL files.\n\n"
                f"```\n{actionable}\n```\n"
            )
        else:
            # DEV-547: compiled, but on proof the code contradicts itself.
            _write_artifact(spec_dir, "build_warnings.txt", build_output)
            feedback = _build_warning_feedback(blocking_warnings)
            logger.warning(
                "spec %s: build compiled but %d blocking warning(s) on "
                "generated files — rotating implementer: %s", spec.id,
                len(blocking_warnings),
                "; ".join(f"{w.located()} {w.message}"
                          for w in blocking_warnings[:3]))
        # DEV-468: when the same diagnostics survive repeated implementer
        # attempts, the implementer is not the author of the defect — the
        # design is, and it is re-read unchanged on every retry, so no number
        # of implementer attempts can converge. Send it upstream instead.
        #
        # On spec_cc7dd609 three of five attempts were spent reproducing three
        # Swift errors that were in the approved design: `mutating` on a
        # `final class`, a `static` split with no receiver, and a reference to
        # an undeclared nested type. Each retry read the design, wrote them
        # again, and failed identically.
        #
        # Not reached on the DEV-547 warning path: _persistent_diagnostics
        # reads `error:` lines, and a warning-only rejection has none, so the
        # call would always return False. Skipping it says so out loud rather
        # than relying on that coincidence. A warning that survives repeated
        # attempts is just as much a design defect, but teaching the persistence
        # detector about warnings is DEV-529's shape of work, not this ticket's.
        if build_reason is not None and _route_build_failure_to_architect(
                db, spec, task, spec_dir, feedback, build_reason):
            return

        # The budget applies here exactly as it does on the parse-failure and
        # test-failure paths. Without this check the short-circuit loops past
        # MAX_RETRIES forever — each pass costs a full generation plus a runner
        # dispatch, and the spec can never reach the synthesis escape hatch
        # that exists precisely for "every attempt failed differently".
        if task.retry_count >= MAX_RETRIES:
            logger.error("spec %s: build-failure retry budget exhausted "
                         "(%d/%d) — handing to synthesis",
                         spec.id, task.retry_count, MAX_RETRIES)
            reviewer_tasks = db.list_tasks_for_spec_by_role(spec.id, "reviewer")
            reviewer_task = reviewer_tasks[0] if reviewer_tasks else None
            if reviewer_task is None:
                db.update_task_status(task.id, TaskStatus.FAILED)
                db.update_spec_status(spec.id, SpecStatus.FAILED)
            else:
                # Same escape hatch the other two exhaustion paths use
                # (DEV-433): the attempts on disk may still merge into
                # something that builds.
                _legacy_attempt_retry(db, spec, reviewer_task, feedback)
            return

        synth_gate = db.create_gate(
            spec_id=spec.id,
            task_id=task.id,
            gate_type=GateType.CODE_REVIEW,
            prompt_md=("## Automated build-failure retry (DEV-429)"
                       if build_reason is not None else
                       "## Automated build-warning retry (DEV-547)"),
        )
        db.respond_to_gate(synth_gate.id, "rejected", notes=feedback)
        db.increment_task_retry(task.id)
        db.update_task_status(task.id, TaskStatus.PENDING)
        logger.info("spec %s: %s, rotating implementer "
                    "without a human gate (attempt %d/%d)", spec.id,
                    f"build failed ({build_reason})" if build_reason is not None
                    else (f"build compiled with "
                          f"{len(blocking_warnings)} blocking warning(s)"),
                    task.retry_count + 1, MAX_RETRIES)
        return

    # DEV-427: the dispatch drops off-limits files so the worktree keeps the
    # base_ref version, but the reviewer still needs telling — the implementer
    # believed it was writing them, and a silent discard is its own surprise.
    protected_touched = []
    if isinstance(ts_for_build, dict):
        protected = {str(p).strip().lstrip("./")
                     for p in (ts_for_build.get("protected_paths") or []) if p}
        protected_touched = sorted(p for p, _ in result.files if p in protected)
    protected_block = ""
    if protected_touched:
        protected_block = (
            "\n\n⚠ **OFF-LIMITS FILES MODIFIED** — the spec puts these out of "
            "bounds. Each was restored to the base revision and the "
            "implementer's version was discarded, so the suite ran against the "
            "original:\n\n"
            + "\n".join(f"- `{p}`" for p in protected_touched) + "\n"
        )
        logger.warning("spec %s: implementer modified %d protected path(s): %s",
                       spec.id, len(protected_touched),
                       ", ".join(protected_touched))

    # Create code_review gate
    file_list = "\n".join(f"- `{p}`" for p, _ in result.files)
    if restored:
        file_list += "\n" + "\n".join(
            f"- `{p}` (restored from a prior attempt's snapshot — not "
            f"regenerated this round)" for p in restored)
    missing_block = ""
    if still_missing:
        missing_block = (
            "\n\n⚠ **MANIFEST FILES MISSING FROM THE WORKSPACE** — the "
            "implementer never produced these declared files and no snapshot "
            "has them; tests that import them can only fail:\n\n"
            + "\n".join(f"- `{p}`" for p in still_missing) + "\n"
        )
    # DEV-478: give the reviewer the runner's own words, not just a verdict.
    # Without this the gate says "the suite has failing tests" and names none
    # of them, and the only other copy is the artifact written above.
    build_excerpt = ""
    if build_passed is False and build_output:
        build_excerpt = (
            "\n<details><summary>Build check output</summary>\n\n"
            f"```\n{_extract_actionable_test_output(build_output, build_framework)}\n```\n"
            "\n</details>\n"
        )
    db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
    db.create_gate(
        spec_id=spec.id,
        task_id=task.id,
        gate_type=GateType.CODE_REVIEW,
        prompt_md=(
            f"## Code review: {spec.title}\n\n"
            f"Spec ID: `{spec.id}`\n"
            f"Retry: {task.retry_count}\n"
            f"{_build_check_line(build_passed, build_output, build_framework)}\n"
            f"The implementer produced the following files:\n\n{file_list}\n"
            f"{missing_block}{protected_block}{build_excerpt}\n"
            f"Approve to proceed to testing, or reject with notes.\n"
        ),
    )
    logger.info("spec %s: implementer done (%d files, retry=%d), "
                "code_review gate created",
                spec.id, len(result.files), task.retry_count)


# Pytest summary line: e.g. "1 passed in 0.01s", "2 failed, 3 passed in 0.5s",
# "5 errors in 1.0s". We only care that *some* outcome count is reported.
_PYTEST_SUMMARY_RE = re.compile(
    r"\b\d+\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b",
    re.IGNORECASE,
)
# Jest summary: "Tests: N passed, M total"
_JEST_SUMMARY_RE = re.compile(r"Tests?:\s+\d+\s+\w+", re.IGNORECASE)
# Node's built-in test runner (node:test) TAP footer: lines like
# "# tests 2", "# pass 1", "# fail 1". Any one of these confirms a real run.
_NODE_TEST_SUMMARY_RE = re.compile(r"^# (?:tests|pass|fail)\s+\d+", re.MULTILINE)
# Vitest summary block (DEV-104):
#     Test Files  1 passed (1)
#          Tests  3 passed (3)
# Deliberately anchored on the "Tests"/"Test Files" counter rather than the
# `Test Files` line alone: vitest prints "Test Files  no tests" when it
# collects nothing, which must NOT read as a successful run. Requiring a
# digit-led outcome means an empty collection fails the guard.
_VITEST_SUMMARY_RE = re.compile(
    r"^\s*(?:Test Files|Tests)\s+\d+\s+(?:passed|failed|skipped|todo)",
    re.MULTILINE,
)

# DEV-429: signatures of a *build* failure, as opposed to a test failure. The
# distinction matters because a build failure needs no human judgement — the
# compiler already said what is wrong — so it must never open a code_review
# gate. Swift emits `path:line:col: error: message` for every diagnostic and
# prints nothing of the sort when the build succeeds and only assertions fail.
# Python's equivalent is a collection/import error, which likewise means the
# suite never ran.
_BUILD_FAILURE_RES = {
    "swift_test": re.compile(r"^.*:\d+:\d+: error: |^error: ", re.MULTILINE),
    "xcodebuild_test": re.compile(
        r"^.*:\d+:\d+: error: |^error: |The following build commands failed",
        re.MULTILINE),
    "pytest": re.compile(
        r"^E\s+(?:ImportError|ModuleNotFoundError|SyntaxError|IndentationError|NameError)|"
        r"^ERROR collecting |^!+ Interrupted: \d+ errors? during collection",
        re.MULTILINE),
}
_BUILD_FAILURE_RES["python"] = _BUILD_FAILURE_RES["pytest"]


# DEV-435: an `error:` the compiler emitted with no file:line in front of it.
# `swift build` reports a failed emit-module job as a bare
# "error: emit-module command failed with exit code 1 (use -v to see
# invocation)" and swallows the underlying diagnostic. Nothing downstream can
# act on that: the retry's file selection keys off cited paths and finds none,
# so it attributes the failure entirely to whatever cascade errors DID carry a
# location — usually the test files that can no longer see the module.
_ATTRIBUTED_ERROR_RE = re.compile(r"^\s*\S.*?:\d+:\d+: error: ", re.MULTILINE)
_BARE_ERROR_RE = re.compile(r"^error: (.+)$", re.MULTILINE)


def _unattributed_errors(output: str) -> list[str]:
    """`error:` lines carrying no file:line, oldest first, deduped."""
    if not output:
        return []
    seen, out = set(), []
    for match in _BARE_ERROR_RE.finditer(output):
        msg = match.group(1).strip()
        if msg and msg not in seen:
            seen.add(msg)
            out.append(msg)
    return out


def _diagnostic_completeness_note(output: str) -> str:
    """Warn, in the failure report, that the diagnostics are incomplete.

    Without this the cascade errors read as the whole story, and both the model
    and the human draw confident conclusions from a build log whose actual
    cause was never printed.
    """
    bare = _unattributed_errors(output)
    if not bare:
        return ""
    has_located = bool(_ATTRIBUTED_ERROR_RE.search(output))
    lines = "".join(f"  - {m}\n" for m in bare)
    note = (
        "⚠ **INCOMPLETE DIAGNOSTICS** — the build reported "
        f"{len(bare)} error(s) with no file or line:\n\n{lines}\n"
    )
    if has_located:
        note += (
            "The located errors below may be *consequences* of these rather "
            "than the cause. In particular, a failed `emit-module` means the "
            "module was never produced, so every 'cannot find X in scope' in "
            "the test target follows from it and fixing the tests will not "
            "help. Look for the defect in the module's own sources.\n"
        )
    else:
        note += (
            "No located error was reported at all, so the failing file is "
            "unknown. Re-examine the sources named in the design.\n"
        )
    return note + "\n"


# ── Built, then died: not the same as never built (DEV-548) ─────────────────
#
# `_BUILD_FAILURE_RES["swift_test"]` accepts a bare `^error: ` line, which is
# what catches the unattributed compile-stage failures DEV-435 documented
# (`error: emit-module command failed`, `error: fatalError`). It also matches
# anything the *test harness* prints, including long after the build finished.
#
# Run 9 of DEV-102 ended with `Build complete! (3.00s)`, 19 tests launched, and
# then `error: Process '…swiftpm-testing-helper…' exited with unexpected signal
# code 5`. That was classified as "the code does not compile" — told to the
# model in those words, while the only located evidence in the output was the
# warning DEV-547 parses. Ordering is the cheap discriminator: an unattributed
# error *after* a completed build is not the compiler rejecting the code.
_BUILD_COMPLETE_RES = {
    "swift_test": re.compile(r"^Build complete!", re.MULTILINE),
    "xcodebuild_test": re.compile(
        r"^\*\* BUILD SUCCEEDED \*\*|^Build complete!", re.MULTILINE),
}

# A diagnostic that names a file:line:col is always the compiler talking.
_LINE_ATTRIBUTED_RE = re.compile(r":\d+:\d+: (?:error|warning): ")

# Driver lines that name a *compile* stage stay build failures wherever they
# appear, so DEV-435's case is untouched. `fatalError` is the swift driver
# reporting a crashed sub-job during the build and is kept here deliberately:
# demoting it would change today's behaviour on every failed Swift build.
_COMPILE_STAGE_ERROR_RE = re.compile(
    r"error: .*\b(?:emit-module|compile|link|build)\b.*command failed"
    r"|error: fatalError"
    r"|The following build commands failed")

_TEST_PROCESS_CRASH_RE = re.compile(
    r"^error: Process '(?P<proc>[^']*)' exited with unexpected signal code "
    r"(?P<signal>\d+)", re.MULTILINE)


def _detect_test_process_crash(output: str) -> str | None:
    """Short reason when the test binary died on a signal (DEV-548).

    This is a third outcome beside "failed to build" and "tests failed": the
    code compiled, the harness started, and the process was killed before it
    could report. Under `--parallel` a single trap takes every test down with
    it, which is why run 9 produced 19 started and 0 completed.

    A behavioural defect, not a build one — an out-of-range subscript, a
    force-unwrapped nil, a failed precondition.
    """
    if not output:
        return None
    match = _TEST_PROCESS_CRASH_RE.search(output)
    if match is None:
        return None
    return (f"the test process exited on signal {match.group('signal')} "
            f"before any test reported")


def _detect_build_failure(output: str, framework: str, passed: bool) -> str | None:
    """Return a short reason when *output* shows the code never built.

    Only ever consulted on a failing run: a green suite proves the build was
    fine, and some tests legitimately print the word "error" in their own
    output. Returning None means "this is a real test failure, or we cannot
    tell" — both of which keep the normal gate path.

    DEV-548: an unattributed `error:` printed after the build completed is not
    a build failure. It is some later process exiting non-zero, and calling it
    a compile failure makes the pipeline state something false to the model.
    """
    if passed or not output:
        return None
    pattern = _BUILD_FAILURE_RES.get(framework.lower())
    if pattern is None:
        return None
    complete = _BUILD_COMPLETE_RES.get(framework.lower())
    complete_match = complete.search(output) if complete else None
    completed_at = complete_match.start() if complete_match else None

    for match in pattern.finditer(output):
        line = output[match.start():].splitlines()[0].strip()
        if not line:
            continue
        # The compiler naming a file, or a driver naming a compile stage:
        # a build failure wherever it appears.
        if (_LINE_ATTRIBUTED_RE.search(line)
                or _COMPILE_STAGE_ERROR_RE.search(line)):
            return line[:200]
        # Bare `error:` after the build finished — a later process failing,
        # not the compiler. Keep looking; something earlier may be real.
        if completed_at is not None and match.start() > completed_at:
            continue
        return line[:200]
    return None


# DEV-468: how many consecutive implementer attempts may produce the same
# diagnostics before the failure is treated as upstream. 1 means the second
# identical build failure goes to the architect.
BUILD_FAILURE_ARCHITECT_THRESHOLD = int(
    os.getenv("AUTONOMOUS_BUILD_FAILURE_ARCHITECT_THRESHOLD", "1"))


def _route_build_failure_to_architect(db: Database, spec: Spec, task, spec_dir,
                                      feedback: str, build_reason: str) -> bool:
    """Send a recurring build failure to the architect. True if it was routed.

    "Retry the implementer" is the wrong response when the implementer is
    faithfully writing what the design told it to. The design is re-read
    unchanged on every attempt, so those errors cannot self-correct — they
    just consume the budget.

    Bounded on both sides: the architect must exist and still be within
    MAX_RETRIES, and the implementer's own budget is left untouched, since
    this attempt was not its fault.
    """
    persistent = _persistent_diagnostics(
        db, spec.id, feedback, lookback=BUILD_FAILURE_ARCHITECT_THRESHOLD)
    if not persistent:
        return False

    architects = db.list_tasks_for_spec_by_role(spec.id, "architect")
    architect = architects[0] if architects else None
    if architect is None:
        return False
    if architect.retry_count >= MAX_RETRIES:
        logger.warning("spec %s: %d diagnostic(s) persist but the architect is "
                       "out of revisions (%d/%d) — continuing with the "
                       "implementer", spec.id, len(persistent),
                       architect.retry_count, MAX_RETRIES)
        return False

    survived = "\n".join(f"  - {m}" for m in sorted(persistent))
    note = (
        "## The design cannot be built as written\n\n"
        f"These diagnostics have survived {BUILD_FAILURE_ARCHITECT_THRESHOLD + 1} "
        f"consecutive implementer attempts, across more than one agent, through "
        f"full regeneration of every file:\n\n{survived}\n\n"
        "That is not an implementation slip. The implementer is writing what "
        "this design specifies, and the design is re-read unchanged on every "
        "attempt, so retrying it cannot help.\n\n"
        f"First diagnostic this round:\n\n    {build_reason}\n\n"
        "Revise the design so the code it describes can actually compile in "
        "the target language. Check every type signature, access level and "
        "member reference in the document against the language's rules. Change "
        "only what the diagnostics require — the behavioural invariants in this "
        "design have already been reviewed and approved.\n\n"
        f"{feedback}"
    )
    try:
        (spec_dir / "design_review_feedback.md").write_text(note)
    except OSError as e:
        logger.warning("spec %s: could not persist build-failure feedback for "
                       "the architect: %s", spec.id, e)
        return False

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer",
                             "model_call": False,
                             "routed_to": "architect",
                             "reason": "persistent_build_diagnostics",
                             "persistent": sorted(persistent)[:8],
                             "diagnostic": build_reason[:200]})
    db.increment_task_retry(architect.id)
    db.update_task_status(architect.id, TaskStatus.PENDING)
    # The implementer re-runs after the new design; its budget is untouched.
    db.update_task_status(task.id, TaskStatus.PENDING)
    logger.warning("spec %s: %d diagnostic(s) survived %d consecutive attempts "
                   "— returning to the architect (revision %d/%d) instead of "
                   "retrying the implementer: %s", spec.id, len(persistent),
                   BUILD_FAILURE_ARCHITECT_THRESHOLD + 1,
                   architect.retry_count + 1, MAX_RETRIES,
                   sorted(persistent)[0][:90])
    return True


# Swift emits no summary that _validate_test_output_structure knows — it
# deliberately passes unrecognised frameworks through, which is right for the
# anti-hallucination guard (a false negative there would force a genuinely
# passing suite to FAIL) but useless for deciding what to tell a reviewer.
# swift-testing prints "✔/✘ Test run with N tests passed/failed after ..."
# and XCTest prints "Executed N tests, with M failures" plus "Test Suite '...'
# passed/failed". Matching any of them is evidence tests actually executed.
_SWIFT_SUMMARY_RE = re.compile(
    r"Test run with \d+ test"
    r"|Executed \d+ test"
    r"|Test Suite '[^']*' (?:passed|failed)",
    re.MULTILINE)


def _observed_a_test_run(output: str, framework: str) -> bool:
    """Is there positive evidence the runner executed tests? (DEV-477)

    Wording only — this never changes control flow, so a miss costs a vaguer
    gate prompt rather than a wrongly-failed suite. That asymmetry is
    deliberate: no Swift suite has ever passed on this pipeline, so the Swift
    patterns above are unvalidated against a real green run, and the only safe
    place for an unvalidated pattern is somewhere it cannot fail the build.
    """
    if not output or not output.strip():
        return False
    ok, _ = _validate_test_output_structure(output, framework)
    if not ok:
        return False
    if framework.lower() in ("swift_test", "xcodebuild_test"):
        return bool(_SWIFT_SUMMARY_RE.search(output))
    return True


def _build_check_line(build_passed: bool | None, build_output: str = "",
                      framework: str = "") -> str:
    """One-line build status for the code_review gate prompt (DEV-429).

    Tells the reviewer what the compiler already established, so a gate is
    never mistaken for "nobody has run this yet".

    DEV-477: a failed check is *not* evidence of a successful compile. It means
    only that the run did not pass and _detect_build_failure recognised no
    diagnostic — which is equally true of a runner dispatch error, a worktree
    that failed to materialise, a timeout, or a sandbox refusal. None of those
    built anything. Claiming "compiled" there is worse than saying nothing: it
    tells the reviewer not to look for build errors. On spec_8dac1142 that put
    five Swift errors in front of a human under an assurance they could not be
    there. So the "tests failed on behaviour" wording is only used when the
    output actually parses as a test run; otherwise say it is unverified.
    """
    if build_passed is None:
        return "Build check: not run (no test framework in the plan).\n"
    if build_passed:
        return "Build check: **compiled and the suite passed.**\n"
    if _observed_a_test_run(build_output, framework):
        return ("Build check: **compiled**, but the suite has failing tests — "
                "the failures are behaviour, not a build error.\n")
    # DEV-538: a gate reached this way has already been requeued to the cap, so
    # say what is actually wrong. Naming it a code review invites someone to
    # review the code, when the code is not the thing that failed and no
    # reading of it can settle the question.
    if test_runner.is_runner_unreachable(build_output):
        return ("Build check: **could not run — the Mac runner was "
                "unreachable.** This is an infrastructure fault, not a verdict "
                "on the code, and it was retried to the requeue cap before "
                "reaching you. Nothing here has been compiled. Bring the "
                "runner back and reject this gate to retry; do not approve it "
                "on a reading of the diff.\n")
    # DEV-548: "no summary" is not automatically "we learned nothing". A build
    # that completed and then took a signal tells us a great deal — the code
    # compiles and traps at runtime — and calling that inconclusive would send
    # a reviewer looking for a build problem that does not exist.
    crash = _detect_test_process_crash(build_output)
    if crash:
        return (f"Build check: **compiled, then crashed** — {crash}. The build "
                f"succeeded, so this is a runtime defect, not a build error. "
                f"No test reported a result, so the suite proves nothing "
                f"either way: under a parallel runner a single trap "
                f"(out-of-range index, force-unwrapped nil, failed "
                f"precondition) takes every test down with it.\n")
    return ("Build check: **inconclusive** — the runner returned neither a test "
            "summary nor a recognised compiler diagnostic, so nothing here "
            "confirms the code builds. Treat the build as unverified.\n")


def _extract_actionable_test_output(output: str, framework: str, max_chars: int = 8000) -> str:
    """Trim test output to the actionable parts for implementer retry feedback.

    Verbose pytest output is mostly noise (collection, plugin versions, progress
    markers) and the actionable bits (assertion tracebacks + the short summary)
    are at the bottom. The previous 3000-char truncation often cut OFF the
    failures and left the implementer with only the preamble — leading to
    repeated retries that converged on the same wrong implementation. Drop the
    preamble; keep the failures and the final summary.

    For unrecognized frameworks, fall back to a tail-biased truncation.
    """
    if not output:
        return output
    fw = framework.lower()
    if fw in ("pytest", "python"):
        # Pytest's failure section starts with `=========== FAILURES ===========`.
        # Anything before that is collection/progress noise.
        marker = output.find("FAILURES")
        if marker != -1:
            # Step back to the start of the line to keep the banner intact.
            line_start = output.rfind("\n", 0, marker) + 1
            extracted = output[line_start:]
            if len(extracted) <= max_chars:
                return extracted
            # Keep the head (first failures) + the tail (summary) — the middle
            # is just more failures of the same kind in most cases.
            head = extracted[: max_chars - 1200]
            tail = extracted[-1200:]
            return head + "\n\n[... output truncated ...]\n\n" + tail
    elif fw in ("jest", "vitest"):
        # Both label a failing suite with a line starting `FAIL <path>`, and
        # both put the per-assertion diagnostics after it. The preamble before
        # the first FAIL is transform/config chatter the implementer cannot act
        # on, and on a large suite it is big enough to push the real failures
        # past a tail-only truncation (DEV-104) — the same trap the pytest
        # branch above exists to avoid.
        marker = output.find("\nFAIL ")
        if marker != -1:
            extracted = output[marker + 1:]
            if len(extracted) <= max_chars:
                return extracted
            head = extracted[: max_chars - 1200]
            tail = extracted[-1200:]
            return head + "\n\n[... output truncated ...]\n\n" + tail
    elif fw == "node_test":
        # node:test emits TAP: failures are `not ok N - name` lines followed by
        # a YAML diagnostic block; passes (`ok N`) before them are just noise
        # for retry feedback. Anchor on the first failure, keep the summary tail.
        marker = output.find("\nnot ok ")
        if marker != -1:
            extracted = output[marker + 1:]
            if len(extracted) <= max_chars:
                return extracted
            head = extracted[: max_chars - 1200]
            tail = extracted[-1200:]
            return head + "\n\n[... output truncated ...]\n\n" + tail
    # Default: tail-biased — the summary is at the end and matters most.
    if len(output) <= max_chars:
        return output
    return "[... output truncated ...]\n\n" + output[-max_chars:]


def _validate_test_output_structure(test_output: str, framework: str) -> tuple[bool, str]:
    """Confirm the test output has the structural shape of a real test run.

    Catches the failure mode where a sandbox error or collection failure exits
    cleanly without any tests actually running, leaving the orchestrator with
    no evidence either way. Returns (ok, reason). On (False, reason) the
    caller should force tests_passed=False — the runner can't be trusted.

    Frameworks we don't recognize (Swift via mac-runner, custom) pass through.
    """
    fw = framework.lower()
    if fw in ("pytest", "python"):
        if not _PYTEST_SUMMARY_RE.search(test_output):
            return False, "no pytest summary line ('N passed/failed/error') detected"
    elif fw == "jest":
        return True, ""
    elif fw == "vitest":
        if not _VITEST_SUMMARY_RE.search(test_output):
            return False, "no vitest summary line ('Tests  N passed') detected"
    elif fw == "node_test":
        if not _NODE_TEST_SUMMARY_RE.search(test_output):
            return False, "no node:test summary line ('# tests/# pass/# fail N') detected"
    return True, ""


# Paths that carry tests, across the frameworks this pipeline dispatches:
# a tests/ or Tests/ directory component, pytest's test_*.py, Swift's
# *Tests.swift, and the JS *.test.* / *.spec.* conventions.
_TEST_PATH_RE = re.compile(
    r"(^|/)[Tt]ests?/|(^|/)test_[^/]+\.py$|[Tt]ests\.swift$|\.(test|spec)\.[jt]sx?$"
)


def _workspace_has_test_files(
    code_files: "list[tuple[str, str]]",
    reviewer_test_files: "list[tuple[str, str]]",
) -> bool:
    """Whether any test file exists to run — from EITHER role (DEV-513).

    The invariant this protects is "never report PASS on a suite that does not
    exist". The original form asked whether the *reviewer* emitted tests, which
    is a different question: when the implementer has already written them and
    the reviewer correctly judges coverage complete, that reads a correct
    judgement as an unrun suite. It fired twice in production and discarded a
    verified-green run both times — the second time on a reviewer that declined
    to add tests by explicit reference to the spec's own artifact-hygiene
    criterion.

    `_run_reviewer_tests` runs whatever is in the spec dir, and the
    implementer's tests are in the spec dir, so the reviewer adding nothing is
    no obstacle to running them.
    """
    if reviewer_test_files:
        return True
    return any(_TEST_PATH_RE.search(path) for path, _ in code_files)


def _collect_reviewer_code_files(db: Database, spec_id: str,
                                 spec_dir) -> list[tuple[str, str]]:
    """Gather implementer code artifacts as (path, content) for the reviewer.

    Binary deliverables (icons, fonts, etc.) get a placeholder so the reviewer
    still sees the path in `## Implementation Files` (preserves rule-5
    cite-check and rule-6 file-list hygiene) without crashing on a UTF-8 decode.
    """
    # Artifact rows accumulate across retries (the retry wipe deletes files,
    # not rows), so each path can have N+1 rows after N retries. Reading all
    # of them duplicated every file N+1 times in the reviewer prompt —
    # ballooning context on exactly the specs already in a retry loop
    # (DEV-143). Rows are created_at-ordered; keep the latest per path.
    latest_by_path: dict = {}
    for art in _list_code_artifacts(db, spec_id):
        latest_by_path[art.path] = art

    code_files = []
    for art in latest_by_path.values():
        fpath = spec_dir / art.path
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text()
        except UnicodeDecodeError:
            content = (
                f"[binary file, {fpath.stat().st_size} bytes — "
                f"reviewer cannot inspect content]"
            )
        code_files.append((art.path, content))
    return code_files


_HARNESS_ERROR_RE = re.compile(
    r"is not a function|is not defined|Cannot find module"
    r"|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|ImportError while importing"
    r"|error(?:s)? during collection")


def _detect_harness_defect(test_output: str, framework: str) -> "str | None":
    """A zero-pass run whose every failure is a harness-class error.

    spec_96d7e07f's reviewer tests did `import { assert } from 'node:test'`
    (no such export), so all 19 tests died on TypeError before exercising a
    line of implementation — and the retry loop burned implementer attempts
    on it. Such a run proves nothing about the code under test, so it must
    not be treated as a logic failure.

    Deliberately conservative: only fires when NOTHING passed and every
    captured error matches the harness pattern (bad import, missing module,
    undefined function). Genuine assertion failures never match. Returns a
    one-line reason, or None.
    """
    if framework in ("node_test", "jest", "vitest"):
        summary = re.search(r"^# pass (\d+)$", test_output, re.MULTILINE)
        fails = re.search(r"^# fail (\d+)$", test_output, re.MULTILINE)
        if not summary or not fails:
            return None
        if int(summary.group(1)) != 0 or int(fails.group(1)) == 0:
            return None
        errors = re.findall(r"error: '([^']+)'", test_output)
        # Suite-level wrappers report their children's count, not a defect.
        errors = [e for e in errors if "subtests failed" not in e]
        if not errors or not all(_HARNESS_ERROR_RE.search(e) for e in errors):
            return None
        distinct = sorted(set(errors))[:3]
        return ("every test failed with a harness-class error, none of the "
                "implementation was exercised: " + "; ".join(distinct))
    if framework in ("pytest", "python"):
        if re.search(r"\b[1-9]\d* passed", test_output):
            return None
        collect = re.search(
            r"(ModuleNotFoundError|ImportError while importing|"
            r"error(?:s)? during collection)", test_output)
        if collect:
            return f"test collection failed before any test ran: {collect.group(1)}"
    return None


def _run_tests_with_guard(spec_id, spec_dir, framework, test_strategy, *,
                          output_label, fail_log):
    """Run the suite and apply the Layer-1 anti-hallucination structural guard.

    A runner that exits 0 with no parseable summary line short-circuited
    (sandbox error, no tests collected, output truncation); trusting the
    subprocess returncode alone let a hallucinated PASS propagate while the
    tests never executed (the bwrap/AppArmor case). An output that doesn't match
    the framework's summary shape is forced to FAIL.

    Returns (passed, output). No DB/artifact side effects — callers persist with
    phase-specific payloads. ``output_label`` / ``fail_log`` keep the persisted
    text and log line identical to each call site's original wording.
    """
    # Pass through framework-specific options from the planner's test_strategy
    # (repo, scheme, destination, etc. for Swift/Xcode via the mac-runner).
    framework_opts = {
        k: v for k, v in test_strategy.items()
        if k not in ("framework", "required")
    }
    passed, output = run_tests(spec_dir, framework=framework, **framework_opts)
    if passed:
        ok, reason = _validate_test_output_structure(output, framework)
        if not ok:
            logger.warning(fail_log, spec_id, reason)
            passed = False
            output = (
                f"[orchestrator guard] {reason}\n\n"
                f"{output_label}\n{output}"
            )
    return passed, output


def _run_reviewer_tests(db: Database, spec: Spec, task, spec_dir,
                        framework, test_strategy):
    """Run the reviewer's tests, apply the structural guard, and persist.

    Returns (tests_passed, test_output); writes test_output.txt and a TEST_RAN
    event as side effects.
    """
    tests_passed, test_output = _run_tests_with_guard(
        spec.id, spec_dir, framework, test_strategy,
        output_label="Original test runner output:",
        fail_log=("spec %s: test_output failed structural validation (%s); "
                  "forcing tests_passed=False to block hallucinated PASS"),
    )
    _write_artifact(spec_dir, "test_output.txt", test_output)
    db.record_event(EventKind.TEST_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"passed": tests_passed,
                             "output_chars": len(test_output)})
    return tests_passed, test_output


def _run_reviewer_adversarial(db: Database, spec: Spec, task, spec_dir,
                              framework, test_strategy, spec_md, design_md,
                              code_files, result, test_output):
    """Phase b: adversarial test generation via Gemini and/or Claude.

    Fires once per spec, only when the Coding Model reviewer's tests pass on retry-0
    (the caller gates entry). Each configured provider runs sequentially; if any
    write tests we re-run the full suite and a failure downgrades the verdict
    (the caller's retry branch picks it up). Fail-open everywhere — per-provider
    exceptions are caught inside generate_adversarial_tests, and the try/except
    here catches anything else (resolve errors, loop bugs) so the original PASS
    stands.

    Returns (tests_passed, test_output): the inputs unchanged unless an
    adversarial rerun supersedes them.
    """
    tests_passed = True
    try:
        adv_results = adversarial.generate_adversarial_tests(
            spec_dir, spec_md, design_md, code_files,
            reviewer_tests=result.test_files,
            reviewer_test_output=test_output,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by design
        logger.warning(
            "spec %s: phase-b dispatch failed (%s: %s); skipping "
            "adversarial tests, original PASS stands",
            spec.id, type(e).__name__, e,
        )
        adv_results = []

    all_adv_files = [
        (path, content)
        for r in adv_results
        for (path, content) in r.files_written
    ]

    if all_adv_files:
        for path, _content in all_adv_files:
            db.create_artifact(spec_id=spec.id, task_id=task.id,
                               kind=ArtifactKind.TEST_REPORT, path=path)

        adv_passed, adv_output = _run_tests_with_guard(
            spec.id, spec_dir, framework, test_strategy,
            output_label="Combined test runner output:",
            fail_log=("spec %s: phase-b combined test_output failed "
                      "structural validation (%s); forcing adv_passed=False"),
        )

        # One AGENT_RAN event per provider so the stats script can
        # attribute false-FAILs back to a specific model. `passed` is
        # the combined-run outcome — same value across providers in a
        # given firing, but each event stays self-contained for
        # querying.
        for r in adv_results:
            db.record_event(
                EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                payload={"role": "adversarial_test_writer",
                         # provider-qualified: `model` alone collides across
                         # providers, and `agent` is the column every other
                         # role writes, so a per-agent query must find one
                         # spelling here too (DEV-528).
                         "agent": f"{r.provider}:{r.model}",
                         "provider": r.provider,
                         "model": r.model,
                         "duration_ms": r.duration_ms,
                         "tests_added": len(r.files_written),
                         "passed": adv_passed if r.files_written else None,
                         "error": r.error,
                         "skip_reason": ("no_blocks_returned"
                                         if r.skipped else None)},
            )

        # Combined run is now canonical — overwrite test_output.txt and
        # record the rerun outcome so dashboards/audits see the merged
        # truth, not the Coding Model-only first pass.
        test_output = adv_output
        tests_passed = adv_passed
        _write_artifact(spec_dir, "test_output.txt", test_output)
        db.record_event(EventKind.TEST_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"passed": tests_passed,
                                 "output_chars": len(test_output),
                                 "phase": "post_adversarial"})

        providers_summary = ", ".join(
            f"{r.provider}={len(r.files_written)}" for r in adv_results
        )
        if not adv_passed:
            logger.info(
                "spec %s: phase-b adversarial tests FAILED (%s) — "
                "falling through to retry branch with combined output",
                spec.id, providers_summary,
            )
        else:
            logger.info(
                "spec %s: phase-b adversarial tests passed (%s) — "
                "PASS stands", spec.id, providers_summary,
            )
    else:
        # No provider produced files — record per-provider so we can
        # still distinguish "all skipped (rule 6)" from "all errored".
        for r in adv_results:
            db.record_event(
                EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                payload={"role": "adversarial_test_writer",
                         "agent": f"{r.provider}:{r.model}",
                         "provider": r.provider,
                         "model": r.model,
                         "duration_ms": r.duration_ms,
                         "tests_added": 0,
                         "passed": None,
                         "error": r.error,
                         "skip_reason": ("no_blocks_returned"
                                         if r.skipped else None)},
            )

    return tests_passed, test_output


def _run_reviewer(db: Database, spec: Spec, task, spec_dir) -> None:
    spec_md = (spec_dir / spec.source_md_path).read_text()
    design_path = spec_dir / "design.md"
    design_md = design_path.read_text() if design_path.exists() else ""

    code_files = _collect_reviewer_code_files(db, spec.id, spec_dir)

    # Detect test framework from the plan. A plan that is not a mapping, or a
    # test_strategy that came back as a scalar, used to raise here and fail the
    # reviewer task outright; degrade to the pytest default instead.
    test_strategy = _load_plan(spec).get("test_strategy")
    if not isinstance(test_strategy, dict):
        test_strategy = {}
    framework = test_strategy.get("framework", "pytest")
    tests_required = test_strategy.get("required", True)

    # Pull supervisor feedback if this is a reviewer retry. Without this the
    # reviewer reruns blind, regenerating the same broken tests — observed in
    # spec_d8ac6b36 where a missing `import pytest` survived 3 retries.
    rejection_notes = _latest_supervisor_feedback(db, spec.id, target_role="reviewer") \
        if task.retry_count > 0 else None

    messages = build_reviewer_message(spec_md, design_md, code_files,
                                       test_framework=framework,
                                       rejection_notes=rejection_notes)
    meta: dict = {}
    raw = call_agent("reviewer", messages, meta=meta)
    _note_truncation(db, spec, task, "reviewer", meta, executor.REVIEWER_MAX_TOKENS)
    result = parse_reviewer_response(raw)

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "reviewer",
                             "result_kind": type(result).__name__,
                             **executor.agent_event_fields(meta)})

    if isinstance(result, ParseError):
        logger.error("spec %s: reviewer response unparseable%s: %s", spec.id,
                     " (truncated)" if meta.get("truncated") else "", result.reason)
        # Persist the raw response so the operator can post-mortem the
        # parse failure. Without this, ~10 minutes of reviewer compute
        # is opaque after the fact.
        try:
            (spec_dir / "reviewer_failed_response.txt").write_text(
                f"# parse error: {result.reason}\n\n{result.raw}"
            )
        except OSError as e:
            logger.warning("spec %s: could not persist failed reviewer "
                           "response: %s", spec.id, e)
        # Robustness (#2): a truncated/unparseable review must NOT instakill the
        # spec (it used to mark spec FAILED here, bypassing the supervisor). The
        # 122B reviewer's degenerate truncation is intermittent, so re-run the
        # reviewer a bounded number of times; on persistent failure treat it as a
        # soft FAIL and route through the normal retry path (supervisor →
        # implementer retry / design revision / abort).
        if task.retry_count < executor.REVIEWER_PARSE_RETRIES:
            db.increment_task_retry(task.id)
            db.update_task_status(task.id, TaskStatus.PENDING)
            logger.warning("spec %s: re-running reviewer after unparseable output "
                           "(attempt %d/%d)", spec.id, task.retry_count + 1,
                           executor.REVIEWER_PARSE_RETRIES)
            return
        logger.error("spec %s: reviewer unparseable after %d attempt(s) — treating "
                     "as soft FAIL and routing to retry", spec.id,
                     task.retry_count + 1)
        _attempt_retry(
            db, spec, task,
            f"The reviewer could not produce a parseable review after "
            f"{task.retry_count + 1} attempt(s) (likely truncation: {result.reason}). "
            f"The implementation was NOT actually reviewed — re-examine the code, "
            f"or revise the design if the spec is hard to satisfy.",
        )
        return

    if result.duplicate_paths:
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "reviewer",
                                 "model_call": False,
                                 "anomaly": "duplicate_test_paths",
                                 "paths": result.duplicate_paths,
                                 "retry": task.retry_count})

    # Write test files. Defensive normalization: a bare `test_*.py` at
    # spec_dir root gets rewritten to `tests/test_*.py` so reviewer
    # output doesn't clutter the deliverable. Pytest discovers tests
    # recursively from spec_dir so this is purely a hygiene step. The
    # REVIEWER_SYSTEM_PROMPT also asks for `tests/` directly; this is
    # the belt-and-braces against the same training-prior bypass that
    # has been observed dropping explicit prompt rules across today's
    # autonomous runs.
    for rel_path, content in result.test_files:
        normalized = rel_path
        if "/" not in rel_path and re.match(r"^test_.*\.py$", rel_path):
            normalized = f"tests/{rel_path}"
            logger.info(
                "spec %s: normalizing reviewer test path %r -> %r",
                spec.id, rel_path, normalized,
            )
        _write_artifact(spec_dir, normalized, content)
        db.create_artifact(spec_id=spec.id, task_id=task.id,
                           kind=ArtifactKind.TEST_REPORT, path=normalized)

    # Anti-hallucination cite-check: verify each `path:line` reference in
    # the review body against the actual spec dir. Bogus cites are
    # annotated inline; the verdict is left intact (some hallucinations
    # are typos on real bugs, so silent veto is too aggressive). Logs
    # the rate so we can track reviewer reliability over time.
    annotated_md, n_checked, n_unverified = _verify_review_citations(
        result.review_md, spec_dir
    )
    if n_checked > 0:
        logger.info(
            "spec %s: reviewer cite-check %d/%d unverified (%.0f%%)",
            spec.id, n_unverified, n_checked,
            100.0 * n_unverified / n_checked,
        )
    if n_unverified > 0:
        result.review_md = annotated_md

    # Write review report
    _write_artifact(spec_dir, "review_report.md", result.review_md)
    db.create_artifact(spec_id=spec.id, task_id=task.id,
                       kind=ArtifactKind.REVIEW_REPORT, path="review_report.md")

    # Run tests if required.
    #
    # tests_passed starts FALSE (DEV-513). It used to start True and survive
    # untouched whenever the suite was skipped, so a reviewer that returned a
    # well-formed PASS verdict with ZERO <<<FILE:>>> blocks reported
    # "Tests **PASSED**" at the release gate having executed nothing. That made
    # `tests_required` unenforceable: it gated whether the suite RAN, never
    # whether the verdict was allowed to be PASS.
    #
    # The invariant is now: PASS requires positive evidence that tests ran.
    # Absence of evidence is a failure, not a pass. This is the same defect
    # class as DEV-502 (a test file outside its build target compiled as
    # nothing and the suite went green) — a success signal that means nothing.
    tests_passed = False
    test_output = ""
    tests_skipped_reason = ""
    if not tests_required:
        # The plan explicitly waived tests. Trusting the verdict alone is a
        # deliberate operator choice here, not an accident of control flow.
        tests_passed = True
        tests_skipped_reason = "the plan set test_strategy.required = false"
    elif not _workspace_has_test_files(code_files, result.test_files):
        tests_skipped_reason = (
            "no test file exists anywhere in the workspace, so the suite "
            "would be vacuous"
        )
        logger.error(
            "spec %s: tests are required but no test file exists in the "
            "workspace — refusing to report PASS on a vacuous suite (DEV-513)",
            spec.id,
        )
    else:
        tests_passed, test_output = _run_reviewer_tests(
            db, spec, task, spec_dir, framework, test_strategy,
        )

    # Phase b: adversarial test generation. Gated to retry-0 PASS runs; the
    # heavy lifting (and its fail-open handling) lives in the helper.
    if (adversarial.ADVERSARIAL_TESTS_ENABLED
            and tests_passed
            and result.verdict == "PASS"
            and task.retry_count == 0
            and tests_required):
        tests_passed, test_output = _run_reviewer_adversarial(
            db, spec, task, spec_dir, framework, test_strategy,
            spec_md, design_md, code_files, result, test_output,
        )

    # Tests are canonical: a reviewer PASS over a red test run must be
    # unrepresentable (DEV-405 — spec_96d7e07f's attempt-5 failure_report
    # opened "Reviewer verdict: PASS" above failing output). Overridden, not
    # re-asked: the reviewer never sees execution output by design, so
    # re-asking can't change what it knows.
    if not tests_passed and result.verdict == "PASS":
        logger.warning(
            "spec %s: reviewer said PASS but the test run failed — "
            "overriding verdict to FAIL", spec.id)
        result.verdict = "FAIL"
        result.review_md = (
            "**Verdict overridden to FAIL: the reviewer's static review said "
            "PASS, but the test run failed — test results are canonical "
            "(DEV-405).**\n\n" + result.review_md)

    if tests_passed and result.verdict == "PASS":
        # Everything looks good — create release_approval gate
        db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
        db.create_gate(
            spec_id=spec.id,
            task_id=task.id,
            gate_type=GateType.RELEASE_APPROVAL,
            prompt_md=(
                f"## Release approval: {spec.title}\n\n"
                f"Spec ID: `{spec.id}`\n\n"
                f"Tests **PASSED**. Reviewer verdict: **PASS**.\n\n"
                f"### Review Report\n\n{result.review_md}\n\n"
                f"### Test Output\n\n```\n{test_output[:3000]}\n```\n\n"
                f"Approve to mark this spec as DONE, or reject to send "
                f"back to the implementer.\n"
            ),
        )
        logger.info("spec %s: reviewer done, tests passed, "
                    "release_approval gate created", spec.id)
    else:
        # A run where the test harness itself is broken proves nothing about
        # the implementation — route it to a free, pointed harness-fix retry
        # instead of burning a logic attempt (DEV-404).
        harness_reason = _detect_harness_defect(test_output, framework)
        if harness_reason and _harness_retry(
                db, spec, task, spec_dir, harness_reason,
                test_output, framework):
            return
        # Tests failed or reviewer said FAIL — attempt retry. Send the
        # actionable slice of test output (failures + summary) rather than
        # the verbose head, so the implementer's retry sees the real
        # AssertionError lines instead of pytest's collection preamble.
        actionable = _extract_actionable_test_output(test_output, framework)
        # Say WHY when the suite never ran (DEV-513). Without this the report
        # carries an empty output fence, which reads as "the tests ran and
        # printed nothing" — the implementer then hunts a phantom failure
        # instead of the reviewer's missing test files.
        if tests_skipped_reason:
            failure_detail = (
                f"Reviewer verdict: {result.verdict}\n\n"
                f"**No tests were executed** — {tests_skipped_reason}.\n\n"
                f"The plan requires tests, so this cannot be approved on the "
                f"reviewer's verdict alone. Emit the test files the spec's "
                f"acceptance criteria call for, using the framework named in "
                f"the plan.\n\n"
                f"Review:\n{result.review_md}\n"
            )
        else:
            failure_detail = (
                f"Reviewer verdict: {result.verdict}\n\n"
                f"Test output (failures + summary, full output in test_output.txt):\n"
                f"```\n{actionable}\n```\n\n"
                f"Review:\n{result.review_md}\n"
            )
        _write_artifact(spec_dir, "failure_report.md", failure_detail)
        _attempt_retry(db, spec, task, failure_detail)


# ── Supervisor-driven transition layer (Phase 2.5) ──────────────────────────
#
# When SUPERVISOR_ENABLED, _attempt_retry and _handle_gate_rejection consult
# the supervisor agent (coding_model_autonomous.supervisor) to decide the next
# transition instead of routing on hardcoded if/elif by role.
#
# Limitations of the prototype:
#   - request_clarification halts the spec on a CLARIFICATION gate; the human
#     must respond manually. There is no auto-resume that re-invokes the
#     supervisor with the response — that's a follow-up.
#   - retry target_role=architect creates an approved CLARIFICATION gate with
#     the feedback as notes, but the existing build_architect_message() does
#     NOT consume clarification rounds. The audit trail is captured but the
#     architect re-runs without seeing the feedback. The supervisor's system
#     prompt steers it toward `replan` for design-level defects, which is
#     the path that actually plumbs the feedback (via _process_pending_plan).
#   - retry target_role=reviewer just resets reviewer to PENDING; no feedback
#     channel exists for the reviewer.

def _list_artifact_summaries(db: Database, spec_id: str) -> list[dict]:
    """Compact summary of all artifacts on disk — for the supervisor context."""
    spec_dir = db.spec_dir(spec_id)
    out = []
    for art in db.list_artifacts(spec_id):
        full = spec_dir / art.path
        size = full.stat().st_size if full.exists() else None
        item = {"kind": art.kind.value, "path": art.path}
        if size is not None:
            item["bytes"] = size
        out.append(item)
    return out


def _build_supervisor_context(db: Database, spec: Spec, task, outcome: str,
                              *, reviewer_notes: str | None = None,
                              test_output_excerpt: str | None = None,
                              agent_error_excerpt: str | None = None,
                              ) -> "_supervisor.SupervisorContext":
    """Assemble the structured snapshot the supervisor reasons over."""
    transitions_used = db.count_events(
        spec_id=spec.id, kind=EventKind.SUPERVISOR_DECISION,
    )
    return {
        "spec_id": spec.id,
        "spec_title": spec.title,
        "phase": task.role,
        "role": task.role,
        "outcome": outcome,
        "retry_count": task.retry_count,
        "transitions_used": transitions_used,
        "plan_yaml": spec.normalized_yaml,
        "reviewer_notes": reviewer_notes,
        "test_output_excerpt": test_output_excerpt,
        "agent_error_excerpt": agent_error_excerpt,
        "artifacts": _list_artifact_summaries(db, spec.id),
        "prior_decisions": _load_prior_decisions(db, spec.id),
    }












def _retry_role_with_feedback(db: Database, spec: Spec, target_role: str,
                              feedback: str, *, current_task) -> None:
    """Apply a supervisor-issued retry to *target_role*.

    For implementer/reviewer-driven retries that originate from the reviewer
    task (test failure or release rejection), we also reset the current_task
    (typically the reviewer) to PENDING so it re-runs after the implementer.
    """
    role_tasks = db.list_tasks_for_spec_by_role(spec.id, target_role)
    target = role_tasks[0] if role_tasks else None
    if target is None:
        logger.error("spec %s: supervisor said retry %s but no such task; aborting",
                     spec.id, target_role)
        db.update_task_status(current_task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if target.retry_count >= MAX_RETRIES:
        logger.error("spec %s: supervisor said retry %s but retry budget exhausted (%d/%d); aborting",
                     spec.id, target_role, target.retry_count, MAX_RETRIES)
        db.update_task_status(current_task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if target_role == "implementer":
        synth = db.create_gate(
            spec_id=spec.id, task_id=target.id,
            gate_type=GateType.CODE_REVIEW,
            prompt_md="## Supervisor-issued retry",
        )
        db.respond_to_gate(synth.id, "rejected", notes=feedback)
    elif target_role == "architect":
        synth = db.create_gate(
            spec_id=spec.id,
            gate_type=GateType.CLARIFICATION,
            prompt_md="## Supervisor-issued architect retry",
        )
        db.respond_to_gate(synth.id, "approved", notes=feedback)
    # reviewer: no feedback channel; just rerun

    db.increment_task_retry(target.id)
    db.update_task_status(target.id, TaskStatus.PENDING)
    # Reset EVERY downstream task so the chain re-runs against the target's new
    # output. Retrying the architect must also reset the implementer (and
    # reviewer): otherwise the revised design is never re-implemented and the
    # reviewer would judge stale code against a new design. (Previously only the
    # current_task was reset, leaving the implementer DONE on an architect retry.)
    target_rank = _ROLE_ORDER.get(target_role, 0)
    for t in db.list_tasks_for_spec(spec.id):
        if t.id == target.id:
            continue
        if (_ROLE_ORDER.get(t.role, 99) > target_rank
                and t.status not in (TaskStatus.PENDING, TaskStatus.SKIPPED)):
            db.update_task_status(t.id, TaskStatus.PENDING)
    logger.info("spec %s: supervisor retry %s (attempt %d/%d)",
                spec.id, target_role, target.retry_count + 1, MAX_RETRIES)


def _apply_supervisor_decision(db: Database, spec: Spec, task,
                               decision: "_supervisor.SupervisorDecision",
                               *, legacy_feedback: str | None) -> None:
    """Translate a SupervisorDecision into DB state changes.

    `legacy_feedback` is the failure_detail / reviewer_notes the legacy code
    would have used — passed through when the supervisor declines to provide
    its own feedback (defensive default for retry/replan).
    """
    db.record_event(
        EventKind.SUPERVISOR_DECISION,
        spec_id=spec.id, task_id=task.id,
        payload={
            "action": decision.action,
            "target_role": decision.target_role,
            "reason": decision.reason,
            "feedback_to_inject": decision.feedback_to_inject,
        },
    )

    if decision.action == "advance":
        logger.info("spec %s: supervisor → advance: %s", spec.id, decision.reason)
        db.update_task_status(task.id, TaskStatus.DONE)
        return

    if decision.action == "abort":
        logger.warning("spec %s: supervisor → abort: %s", spec.id, decision.reason)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if decision.action == "retry":
        feedback = decision.feedback_to_inject or legacy_feedback or ""
        logger.info("spec %s: supervisor → retry %s: %s",
                    spec.id, decision.target_role, decision.reason)
        _retry_role_with_feedback(db, spec, decision.target_role, feedback,
                                  current_task=task)
        return

    if decision.action == "replan":
        logger.info("spec %s: supervisor → replan: %s", spec.id, decision.reason)
        feedback = decision.feedback_to_inject or legacy_feedback
        if feedback:
            synth = db.create_gate(
                spec_id=spec.id,
                gate_type=GateType.CLARIFICATION,
                prompt_md="## Supervisor-requested replan",
            )
            db.respond_to_gate(synth.id, "approved", notes=feedback)
        # Mark ALL existing tasks SKIPPED (including DONE ones) so the new
        # plan produces a fresh task DAG. Leaving DONE tasks in place causes
        # _process_executing to skip the bootstrap branch and falsely mark
        # the spec DONE without running the new plan.
        for t in db.list_tasks_for_spec(spec.id):
            if t.status != TaskStatus.SKIPPED:
                db.update_task_status(t.id, TaskStatus.SKIPPED)
        db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)
        return

    if decision.action == "request_clarification":
        logger.info("spec %s: supervisor → request_clarification: %s",
                    spec.id, decision.reason)
        db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
        db.create_gate(
            spec_id=spec.id, task_id=task.id,
            gate_type=GateType.CLARIFICATION,
            prompt_md=(
                "## Supervisor needs clarification\n\n"
                f"{decision.feedback_to_inject}\n\n"
                "Approve with notes to provide an answer, or reject to abort."
            ),
        )
        return

    # Defensive: schema validation in supervisor.py should make this unreachable
    logger.error("spec %s: unknown supervisor action %r; aborting",
                 spec.id, decision.action)
    db.update_task_status(task.id, TaskStatus.FAILED)
    db.update_spec_status(spec.id, SpecStatus.FAILED)


# Free (non-budget) harness-fix retries per spec. Capped so a model that
# keeps emitting a broken harness still converges onto the normal retry
# budget instead of looping forever.
_HARNESS_FREE_RETRIES = 2


def _harness_retry(db: Database, spec: Spec, task, spec_dir: Path,
                   reason: str, test_output: str, framework: str) -> bool:
    """Re-run the implementer with a pointed harness-fix instruction WITHOUT
    burning a logic retry (DEV-404). Returns False when the free-retry
    budget for this spec is spent — the caller then falls through to the
    normal (budgeted) retry path.
    """
    counter_path = spec_dir / "harness_retries.json"
    try:
        used = json.loads(counter_path.read_text()).get("used", 0)
    except (OSError, ValueError):
        used = 0
    if used >= _HARNESS_FREE_RETRIES:
        logger.warning("spec %s: harness defect again but free harness "
                       "retries exhausted (%d) — counting against the "
                       "normal budget", spec.id, used)
        return False

    impl_tasks = db.list_tasks_for_spec_by_role(spec.id, "implementer")
    impl_task = impl_tasks[0] if impl_tasks else None
    if impl_task is None:
        return False

    failure_detail = (
        "TEST HARNESS DEFECT — the test files themselves are broken, so the "
        "implementation was never actually exercised:\n\n"
        f"{reason}\n\n"
        "Fix ONLY the test harness; do not rewrite implementation files. "
        "For node:test suites, assertions come from "
        "`import assert from 'node:assert/strict'` — the `node:test` module "
        "does not export `assert`.\n\n"
        f"Test output (failures + summary):\n"
        f"```\n{_extract_actionable_test_output(test_output, framework)}\n```\n"
    )
    _write_artifact(spec_dir, "failure_report.md", failure_detail)
    try:
        counter_path.write_text(json.dumps({"used": used + 1}))
    except OSError as e:
        logger.warning("spec %s: could not persist harness retry counter: %s",
                       spec.id, e)

    # Same feedback channel _retry_role_with_feedback uses for implementer
    # retries — a rejected CODE_REVIEW gate — minus the budget increment.
    gate = db.create_gate(
        spec_id=spec.id, task_id=impl_task.id,
        gate_type=GateType.CODE_REVIEW,
        prompt_md="## Harness-defect retry (does not count against the budget)",
    )
    db.respond_to_gate(gate.id, "rejected", notes=failure_detail)
    db.update_task_status(impl_task.id, TaskStatus.PENDING)
    impl_rank = _ROLE_ORDER.get("implementer", 0)
    for t in db.list_tasks_for_spec(spec.id):
        if (t.id != impl_task.id
                and _ROLE_ORDER.get(t.role, 99) > impl_rank
                and t.status not in (TaskStatus.PENDING, TaskStatus.SKIPPED)):
            db.update_task_status(t.id, TaskStatus.PENDING)
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "harness_guard",
                             "model_call": False,
                             "free_retry": used + 1,
                             "reason": reason[:300]})
    logger.info("spec %s: harness defect — free retry %d/%d issued (%s)",
                spec.id, used + 1, _HARNESS_FREE_RETRIES, reason[:120])
    return True


def _attempt_retry(db: Database, spec: Spec, task, failure_detail: str) -> None:
    """Decide what to do after a test-failure / reviewer-FAIL outcome.

    With SUPERVISOR_ENABLED, asks the supervisor agent; on SupervisorError
    (transport, parse, schema violation), falls back to the legacy retry
    path so a flaky meta-call can't take down the spec.
    """
    if not SUPERVISOR_ENABLED:
        _legacy_attempt_retry(db, spec, task, failure_detail)
        return

    ctx = _build_supervisor_context(
        db, spec, task,
        outcome="test_fail",
        test_output_excerpt=failure_detail,
    )
    try:
        decision = _supervisor.decide(ctx)
    except _supervisor.SupervisorError as e:
        logger.warning("spec %s: supervisor failed (%s); falling back to legacy retry",
                       spec.id, e)
        _legacy_attempt_retry(db, spec, task, failure_detail)
        return
    _apply_supervisor_decision(db, spec, task, decision,
                               legacy_feedback=failure_detail)


_SYNTHESIS_AGENT = os.getenv("AUTONOMOUS_SYNTHESIS_AGENT", "deep_reviewer")

# A synthesized artifact failing only a small minority of tests gets ONE
# targeted repair round before the spec fails (DEV-406 — spec_96d7e07f's
# synthesis scored 15/17 and hard-failed a 2h47m run). >1.0 disables.
_SYNTHESIS_REPAIR_MIN_RATE = float(
    os.getenv("AUTONOMOUS_SYNTHESIS_REPAIR_MIN_RATE", "0.8"))


def _test_pass_rate(test_output: str) -> "float | None":
    """Best-effort pass fraction from a runner summary; None if unparseable.

    None (not 0.0) on no-parse: an unreadable summary must not qualify for
    a repair round it can't be measured against.
    """
    tap_total = re.search(r"^# tests (\d+)$", test_output, re.MULTILINE)
    tap_pass = re.search(r"^# pass (\d+)$", test_output, re.MULTILINE)
    if tap_total and tap_pass and int(tap_total.group(1)) > 0:
        return int(tap_pass.group(1)) / int(tap_total.group(1))
    passed = re.search(r"(\d+) passed", test_output)
    failed = re.search(r"(\d+) failed", test_output)
    if passed:
        n_pass = int(passed.group(1))
        n_fail = int(failed.group(1)) if failed else 0
        if n_pass + n_fail > 0:
            return n_pass / (n_pass + n_fail)
    return None




def _collect_rejection_notes(db: Database, spec_id: str) -> list[str]:
    """Human reviewer notes from rejected CODE_REVIEW gates, oldest first.

    Skips the synthetic gates the daemon answers itself (parse-failure and
    build-failure retries): their content is compiler or parser output that
    already travels with each attempt's test summary, and repeating it here
    would crowd out the human judgement this is for.
    """
    notes = []
    for gate in db.list_gates_for_spec(spec_id, GateType.CODE_REVIEW):
        if gate.status is not GateStatus.REJECTED or not gate.reviewer_notes:
            continue
        if (gate.prompt_md or "").startswith("## Automated"):
            continue
        notes.append(gate.reviewer_notes)
    return notes


def _repair_verdict(repair_passed: bool, pre_diags: list, post_diags: list,
                    new_classes: list, protected_files) -> "tuple[bool, list]":
    """Keep the synthesis repair, or roll it back? (DEV-541)

    Returns ``(improved, protected symbols the repair collided with)``.

    The count of diagnostic OCCURRENCES is the criterion, not the set of
    distinct messages: a dropped import emits many diagnostics repeating one
    message, so a deduplicated set barely moves while the build collapses.

    New diagnostic classes are reported rather than vetoed, because vetoing
    them over-rejects — a repair going 12 → 1 is obviously good even though its
    single survivor is new.

    The exception, added after run 10: a new class naming a symbol declared in
    a PROTECTED file vetoes the repair whatever the count did. Run 10's repair
    went 8 → 7 and was kept, when what it had actually done was invent a file
    redeclaring `Field` from the protected scaffold. Every one of those 7
    diagnostics was its own doing and none was reachable — the file it collided
    with is one the pipeline may not edit, so no later attempt could resolve
    it. A lower count does not make an unrecoverable build progress.
    """
    symbols: set = set()
    for path, content in (protected_files or []):
        if path.endswith(".swift"):
            symbols |= executor.declared_top_level_types(content)
    poisoned = sorted(
        {sym for sym in symbols for cls in new_classes
         if re.search(rf"\b{re.escape(sym)}\b", cls)})
    improved = repair_passed or (len(post_diags) < len(pre_diags)
                                 and not poisoned)
    return improved, poisoned


def _run_synthesis(db: Database, spec: Spec, impl_task, spec_dir: Path,
                   framework: str, framework_opts: dict) -> tuple[bool, str]:
    """MAX_RETRIES escape hatch: synthesize the union of correct behaviors
    across all rotation attempts, then re-run the test phase against the
    synthesized output.

    Returns (passed, test_output): passed is True only if synthesis produced
    output and its tests passed the structural guard. Caller decides what to
    do with the verdict.
    """
    attempts = _read_retry_attempts(spec_dir)
    if not attempts:
        logger.warning("spec %s: synthesis skipped — no retry history", spec.id)
        return False, ""

    spec_md_path = spec_dir / spec.source_md_path
    design_md_path = spec_dir / "design.md"
    spec_md = spec_md_path.read_text() if spec_md_path.exists() else ""
    design_md = design_md_path.read_text() if design_md_path.exists() else ""

    # DEV-433: when the attempts were rejected at the gate rather than by a
    # failing test run, the reviewer's notes are the single most useful input
    # synthesis can have — they say which attempt got which part right, which
    # is exactly the judgement the merge needs and cannot recover from the
    # code alone.
    review_notes = _collect_rejection_notes(db, spec.id)

    logger.info("spec %s: synthesis from %d attempts (%d reviewer notes) "
                "via agent=%s", spec.id, len(attempts), len(review_notes),
                _SYNTHESIS_AGENT)

    # DEV-552: fetched once for the whole synthesis phase — the merge prompt,
    # the repair prompt, and both collision checks all use the same list.
    protected_files = _fetch_protected_files_for_spec(spec)

    messages = build_synthesis_message(spec_md, design_md, attempts,
                                       review_notes=review_notes,
                                       reference_files=protected_files)

    # Synthesis merges every attempt's files into one final response — the same
    # single-call emit-everything constraint as the implementer, so it gets the
    # same design-scaled budget instead of the old hardcoded 16000 (which made
    # the merge step the most truncation-prone call in the pipeline).
    synth_max_tokens = executor.implementer_max_tokens_for(design_md)
    meta: dict = {}
    try:
        raw = call_agent("implementer", messages, agent=_SYNTHESIS_AGENT,
                         max_tokens=synth_max_tokens, meta=meta)
    except Exception as exc:
        logger.error("spec %s: synthesis call failed: %s", spec.id, exc)
        return False, ""
    _note_truncation(db, spec, impl_task, "synthesizer", meta, synth_max_tokens)

    result = parse_implementer_response(raw)
    if isinstance(result, ParseError):
        logger.error("spec %s: synthesis response unparseable: %s",
                     spec.id, result.reason)
        return False, ""

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=impl_task.id,
                    payload={"role": "synthesizer",
                             "attempts": len(attempts),
                             "files": len(result.files),
                             **executor.agent_event_fields(meta)})

    # Wipe the live attempt so synthesis doesn't collide with it. Snapshot
    # first so we keep the corpus.
    _snapshot_retry(spec_dir, retry_index=len(attempts) - 1
                    if attempts and attempts[-1]["agent"] == "current"
                    else len(attempts))
    for path in spec_dir.iterdir():
        if path.name in _PRESERVE_ON_RETRY or path.name == "retry_history":
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            pass

    result.files = _normalize_generated_files(
        db, spec, impl_task, result.files, "synthesizer",
        protected_files=protected_files)
    for rel_path, content in result.files:
        _write_artifact(spec_dir, rel_path, content)
        db.create_artifact(spec_id=spec.id, task_id=impl_task.id,
                           kind=ArtifactKind.CODE, path=rel_path)
    logger.info("spec %s: synthesis wrote %d files", spec.id, len(result.files))

    tests_passed, test_output = _run_tests_with_guard(
        spec.id, spec_dir, framework, framework_opts,
        output_label="Original test runner output:",
        fail_log=("spec %s: synthesis test_output failed structural "
                  "validation (%s); forcing tests_passed=False to block "
                  "hallucinated PASS"),
    )
    try:
        (spec_dir / "test_output.txt").write_text(test_output)
    except OSError:
        pass
    logger.info("spec %s: synthesis test result: %s (%d chars)",
                spec.id, "PASS" if tests_passed else "FAIL", len(test_output))
    if tests_passed:
        return True, test_output

    # A near-miss gets ONE targeted repair round before the spec fails
    # (DEV-406). Bounded strictly: one extra agent call, only above the
    # pass-rate threshold, and a converted pass still flows through the
    # caller's release_approval gate exactly like a first-try synthesis
    # pass — never straight to DONE.
    #
    # DEV-469: a build failure produces no runner summary, so the pass rate is
    # unmeasurable and this used to skip the repair entirely — inverting the
    # intent, because a synthesis that fails to compile is usually *nearer* to
    # right than one that compiles and fails a third of its assertions. It is a
    # type or syntax slip, not a behavioural misunderstanding. That mattered
    # more once DEV-433 made synthesis reachable from build-driven exhaustion,
    # since every failure arriving that way is a build failure by construction.
    # Observed on spec_cc7dd609: synthesis died on two one-line type errors
    # with no repair attempted.
    rate = _test_pass_rate(test_output)
    build_failed = _detect_build_failure(test_output, framework, tests_passed)
    # DEV-547: the unmeasurable case is not always unexplained. Run 9 of
    # spec_9ff962b9 compiled, started all 19 tests and trapped — no summary, so
    # no pass rate, and no compiler error either — while the compiler had named
    # the defect as a warning on the exact line. Consulted only when nothing
    # failed to compile, since a real diagnostic is better feedback.
    warning_blocking: list = []
    if not build_failed and BLOCK_ON_BUILD_WARNINGS:
        warning_blocking = _blocking_build_warnings(
            test_output, (framework_opts or {}).get("protected_paths"))
    if rate is None and build_failed:
        logger.info("spec %s: synthesis failed to build (%s) — one targeted "
                    "repair round", spec.id, build_failed)
    elif rate is None and warning_blocking:
        logger.info("spec %s: synthesis produced no test summary and no "
                    "compiler diagnostic, but %d blocking warning(s) on "
                    "generated code — one targeted repair round: %s",
                    spec.id, len(warning_blocking),
                    "; ".join(f"{w.located()} {w.message}"
                              for w in warning_blocking[:3]))
    elif rate is None:
        # No summary, no compiler diagnostic and nothing the compiler objected
        # to: the runner itself is suspect, which is the hallucinated-PASS case
        # the structural guard handles. Repairing generated code cannot fix
        # that, so do not spend the call.
        logger.info("spec %s: synthesis failure not measurable and shows no "
                    "compiler diagnostic — no repair round", spec.id)
        return tests_passed, test_output
    elif rate < _SYNTHESIS_REPAIR_MIN_RATE:
        logger.info("spec %s: synthesis %.0f%% pass, below the %.0f%% repair "
                    "threshold — no repair round", spec.id, rate * 100,
                    _SYNTHESIS_REPAIR_MIN_RATE * 100)
        return tests_passed, test_output
    else:
        logger.info("spec %s: synthesis near-miss (%.0f%% pass) — one targeted "
                    "repair round", spec.id, rate * 100)
    # DEV-522: tell the repair which kind of failure it is looking at. With
    # `rate is None` there are now two ways to be here — failed to build, or
    # DEV-547's compiled-but-contradicted — and `build_failed` separates them,
    # since the warning branch is only reachable when it is falsy. The two
    # early returns above have already discarded the unexplained and
    # far-from-passing cases. Both existing prompts stay byte-identical.
    repair_messages = executor.build_synthesis_repair_message(
        spec_md, design_md, result.files,
        _extract_actionable_test_output(test_output, framework),
        build_diagnostic=build_failed if rate is None else None,
        warning_diagnostic=(_format_build_warnings(warning_blocking)
                            if rate is None and not build_failed
                            and warning_blocking else None),
        reference_files=protected_files,
    )
    repair_meta: dict = {}
    try:
        repair_raw = call_agent("implementer", repair_messages,
                                agent=_SYNTHESIS_AGENT,
                                max_tokens=synth_max_tokens, meta=repair_meta)
    except Exception as exc:
        logger.error("spec %s: synthesis repair call failed: %s", spec.id, exc)
        return False, test_output
    _note_truncation(db, spec, impl_task, "synthesis_repair", repair_meta,
                     synth_max_tokens)
    repair = parse_implementer_response(repair_raw)
    if isinstance(repair, ParseError):
        logger.warning("spec %s: synthesis repair unparseable (%s) — keeping "
                       "the original failure", spec.id, repair.reason)
        return False, test_output

    # DEV-541: the repair is a proposal, not a commit. Snapshot every path it
    # is about to touch, so a repair that comes back worse can be undone. This
    # is the last operation before the spec dies — whatever sits on disk when
    # this returns is what the run is judged on and what any later replay
    # reads — which makes it the least defensible place to keep unverified
    # output. Run 8 went 3 diagnostics → 14 here, and the 14 were what
    # survived. Note the parse-failure branch just above already declines to
    # overwrite on a bad repair; this extends the same care to a repair that
    # parses cleanly and is simply worse.
    pre_repair_state: dict[str, str | None] = {}
    for rel_path, _ in repair.files:
        try:
            target = executor.artifact_path(spec_dir, rel_path)
        except ValueError:
            continue  # traversal — the write below will reject it too
        pre_repair_state[rel_path] = (
            target.read_text() if target.is_file() else None)
    pre_repair_diags = _attributed_diagnostics(test_output)

    # Overlay only the files the repair emitted; everything else stays.
    repair.files = _normalize_generated_files(
        db, spec, impl_task, repair.files, "synthesis_repair",
        protected_files=protected_files)
    for rel_path, content in repair.files:
        _write_artifact(spec_dir, rel_path, content)
        db.create_artifact(spec_id=spec.id, task_id=impl_task.id,
                           kind=ArtifactKind.CODE, path=rel_path)
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id,
                    task_id=impl_task.id,
                    payload={"role": "synthesis_repair",
                             **executor.agent_event_fields(repair_meta),
                             # None when the repair was triggered by a build
                             # failure, which has no pass rate to report
                             # (DEV-469) — the reason is recorded instead.
                             "pre_repair_pass_rate": (
                                 round(rate, 3) if rate is not None else None),
                             # DEV-547 adds a third value; keeping the two
                             # existing spellings byte-identical so queries
                             # written against them still work.
                             "trigger": ("build_failure"
                                         if rate is None and build_failed
                                         else "build_warning" if rate is None
                                         else "near_miss"),
                             # DEV-522: this used to be a bare `files` count of
                             # what the repair EMITTED, which reads as what the
                             # repair was GIVEN — the repair is handed every
                             # synthesized file. That ambiguity sent a diagnosis
                             # of run 6 straight into a visibility bug that does
                             # not exist, so record both sides explicitly.
                             "files_offered": len(result.files),
                             "files_changed": len(repair.files),
                             "changed_paths": [p for p, _ in repair.files]})
    # `test_output` still holds the PRE-repair failure; the repair's own result
    # goes to a separate name so the rollback below has something to return.
    repair_passed, repair_output = _run_tests_with_guard(
        spec.id, spec_dir, framework, framework_opts,
        output_label="Post-repair test runner output:",
        fail_log=("spec %s: post-repair test_output failed structural "
                  "validation (%s); forcing tests_passed=False"),
    )
    post_repair_diags = _attributed_diagnostics(repair_output)
    new_classes = sorted(set(post_repair_diags) - set(pre_repair_diags))

    # Keep the repair if it passed, or if it strictly reduced the number of
    # diagnostics. Anything else — more of them, or the same count rearranged —
    # is not evidence of progress, and the spec fails either way, so the state
    # with fewer known defects is the one worth keeping.
    #
    # Count occurrences, NOT distinct messages. A dropped import produces many
    # diagnostics repeating one message, so the deduplicated set barely moves
    # while the build collapses — run 8's repair output is 27 diagnostics from
    # 6 distinct messages. Comparing sets would have rolled that particular
    # case back too (6 > 3), but only by luck: a regression whose errors all
    # share a single message reads as an improvement, and there is a test
    # pinning exactly that shape.
    #
    # DEV-541 originally specified "more diagnostics OR any new class". That
    # over-rejects: a repair going 12 → 1 would be discarded merely because the
    # single survivor is new, which is plainly the wrong call. The count is the
    # criterion; new classes are reported, not vetoed.
    #
    # …with one exception, added after run 10 produced the mirror case. That
    # repair went 8 → 7, so count-only KEPT it — while what it had actually
    # done was invent a file redeclaring `Field`, a type owned by a protected
    # scaffold. Every one of those 7 diagnostics was the repair's own doing,
    # and none of them was reachable: the file it collided with is one the
    # pipeline may not edit, so no later attempt could have resolved it either.
    #
    # So a new diagnostic class that names a symbol declared in a protected
    # file vetoes the repair regardless of the count. That is narrow on
    # purpose — it does not touch the 12 → 1 case, whose new class is about
    # ordinary generated code — and it keys on the one property that makes a
    # regression permanent rather than merely bad.
    improved, poisoned = _repair_verdict(
        repair_passed, pre_repair_diags, post_repair_diags, new_classes,
        protected_files)

    if not improved:
        for rel_path, previous in pre_repair_state.items():
            try:
                target = executor.artifact_path(spec_dir, rel_path)
            except ValueError:
                continue
            if previous is None:
                target.unlink(missing_ok=True)  # the repair invented this file
            else:
                target.write_text(previous)
        repair_passed, repair_output = False, test_output

    db.record_event(EventKind.TEST_RAN, spec_id=spec.id, task_id=impl_task.id,
                    payload={"phase": "synthesis_repair",
                             "passed": repair_passed,
                             "errors_before": len(pre_repair_diags),
                             "errors_after": len(post_repair_diags),
                             "new_diagnostic_classes": new_classes,
                             # DEV-541: which protected symbols the repair
                             # collided with, empty when none. A rollback with
                             # a non-empty list is a different animal from one
                             # that merely failed to reduce the count.
                             "protected_symbols_hit": poisoned,
                             "rolled_back": not improved})

    try:
        (spec_dir / "test_output.txt").write_text(repair_output)
    except OSError:
        pass

    if repair_passed:
        outcome = "PASS"
    elif improved:
        outcome = (f"FAIL — repair improved the build "
                   f"({len(pre_repair_diags)} → {len(post_repair_diags)} "
                   f"diagnostics) but the suite still fails")
    elif poisoned:
        outcome = (f"FAIL — repair reduced the count "
                   f"({len(pre_repair_diags)} → {len(post_repair_diags)} "
                   f"diagnostics) but collided with protected symbol(s) "
                   f"{', '.join(poisoned)}; rolled back to the pre-repair "
                   f"state. A collision with a file the pipeline may not edit "
                   f"is unrecoverable, so a lower count does not make it "
                   f"progress")
    else:
        outcome = (f"FAIL — repair did not improve the build "
                   f"({len(pre_repair_diags)} → {len(post_repair_diags)} "
                   f"diagnostics); rolled back to the pre-repair state")
    logger.info("spec %s: synthesis repair result: %s", spec.id, outcome)
    if new_classes:
        logger.info("spec %s: repair introduced %d diagnostic class(es) not "
                    "seen before it ran: %s", spec.id, len(new_classes),
                    "; ".join(new_classes[:3]))
    return repair_passed, repair_output


def _legacy_attempt_retry(db: Database, spec: Spec, task, failure_detail: str) -> None:
    """Send the spec back to the implementer for another attempt, or fail.

    At MAX_RETRIES exhaustion, attempts a synthesis pass first: merges the
    union of correct behaviors across all rotation attempts and re-runs
    the test phase. A passing synthesis goes to a release_approval gate
    (it was assembled after repeated failures and has no reviewer verdict,
    so it must not skip the human gate); only if synthesis also fails does
    the spec get marked FAILED. See project_autonomous_validation_2026_05_04
    for rationale.
    """
    impl_tasks = db.list_tasks_for_spec_by_role(spec.id, "implementer")
    impl_task = impl_tasks[0] if impl_tasks else None

    if impl_task is None:
        logger.error("spec %s: no implementer task to retry", spec.id)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if impl_task.retry_count >= MAX_RETRIES:
        logger.info("spec %s: max retries (%d) exhausted — attempting synthesis",
                    spec.id, MAX_RETRIES)
        # Reconstruct the framework + opts the test phase would have used.
        # Pulled from plan.yaml (test_strategy block).
        framework, framework_opts = "pytest", {}
        ts = _load_plan(spec).get("test_strategy")
        if isinstance(ts, dict):
            framework = ts.get("framework", "pytest")
            framework_opts = {
                k: v for k, v in ts.items()
                if k not in ("framework", "required")
            }
        elif ts is not None:
            logger.warning("spec %s: synthesis: test_strategy is %s, not a mapping; "
                           "defaulting to pytest with no opts",
                           spec.id, type(ts).__name__)

        synth_passed, synth_output = _run_synthesis(
            db, spec, impl_task, db.spec_dir(spec.id), framework, framework_opts)
        if synth_passed:
            db.update_task_status(impl_task.id, TaskStatus.DONE)
            db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
            db.create_gate(
                spec_id=spec.id,
                task_id=task.id,
                gate_type=GateType.RELEASE_APPROVAL,
                prompt_md=(
                    f"## Release approval: {spec.title}\n\n"
                    f"Spec ID: `{spec.id}`\n\n"
                    f"**Synthesized after {MAX_RETRIES} failed retries.** This "
                    f"output merges the passing behaviors of every rotation "
                    f"attempt; tests **PASSED** on it, but it has no reviewer "
                    f"verdict.\n\n"
                    f"### Test Output\n\n```\n{synth_output[:3000]}\n```\n\n"
                    f"Approve to mark this spec as DONE, or reject to fail "
                    f"the spec (implementer retries are exhausted).\n"
                ),
            )
            logger.info("spec %s: synthesis PASSED — release_approval gate "
                        "created", spec.id)
            return

        logger.error("spec %s: synthesis FAILED — marking spec failed", spec.id)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    # Create a synthetic rejected code_review gate with the failure details
    # so _run_implementer picks it up as rejection_notes on its next run.
    synth_gate = db.create_gate(
        spec_id=spec.id,
        task_id=impl_task.id,
        gate_type=GateType.CODE_REVIEW,
        prompt_md="## Automated test failure — retry",
    )
    db.respond_to_gate(synth_gate.id, "rejected", notes=failure_detail)

    db.increment_task_retry(impl_task.id)
    db.update_task_status(impl_task.id, TaskStatus.PENDING)
    db.update_task_status(task.id, TaskStatus.PENDING)  # reviewer re-runs too
    logger.info("spec %s: tests failed, retrying implementer (attempt %d/%d)",
                spec.id, impl_task.retry_count + 1, MAX_RETRIES)


def _latest_task_clarification(db: Database, spec_id: str, task_id: str):
    """Newest CLARIFICATION gate bound to *task_id*.

    Supervisor request_clarification gates are the only CLARIFICATION gates
    created with a task_id; the planner/architect feedback synthetics are
    spec-level (task_id None) and must not match here.
    """
    gates = [g for g in db.list_gates_for_spec(spec_id, GateType.CLARIFICATION)
             if g.task_id == task_id]
    return gates[-1] if gates else None


def _resume_from_clarification(db: Database, spec: Spec, task, answer: str) -> None:
    """A human answered the supervisor's question — feed the answer back.

    Re-invokes the supervisor with outcome=clarification_answered so the
    answer drives the next transition. Before DEV-122 the answer was read
    by nothing: the spec parked in EXECUTING forever.
    """
    logger.info("spec %s: clarification answered for task %s — resuming",
                spec.id, task.id)
    if not SUPERVISOR_ENABLED:
        # Supervisor toggled off since the gate was created. Deterministic
        # un-wedge: re-run the parked phase.
        logger.warning("spec %s: supervisor disabled; re-running %s after "
                       "clarification", spec.id, task.role)
        db.update_task_status(task.id, TaskStatus.PENDING)
        return
    ctx = _build_supervisor_context(
        db, spec, task,
        outcome="clarification_answered",
        reviewer_notes=answer,
    )
    try:
        decision = _supervisor.decide(ctx)
    except _supervisor.SupervisorError as e:
        logger.warning("spec %s: supervisor failed on clarification resume "
                       "(%s); re-running %s", spec.id, e, task.role)
        db.update_task_status(task.id, TaskStatus.PENDING)
        return
    _apply_supervisor_decision(db, spec, task, decision, legacy_feedback=answer)


def _check_execution_gate(db: Database, spec: Spec, task) -> None:
    """Check the review gate for a task in BLOCKED_ON_REVIEW."""
    # A supervisor request_clarification parks the task here with a
    # CLARIFICATION gate bound to it — that gate, not the role's review
    # gate, is what this tick must read. Before DEV-122 only the role gate
    # was consulted, so the human's answer was read by nothing (spec wedged
    # in EXECUTING), while a REJECTED role gate below re-invoked the
    # supervisor every tick until the transition budget aborted the spec.
    clar = _latest_task_clarification(db, spec.id, task.id)
    if clar is not None and clar.status != GateStatus.CANCELLED:
        if clar.status == GateStatus.PENDING:
            return  # waiting on the human's answer
        if clar.status == GateStatus.APPROVED:
            answer = clar.reviewer_notes or ""
            db.cancel_gate(clar.id)  # consume first — never process twice
            _resume_from_clarification(db, spec, task, answer)
            return
        # REJECTED: the gate says "reject to abort".
        db.cancel_gate(clar.id)
        logger.warning("spec %s: clarification rejected by human — aborting",
                       spec.id)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    gate_type = _ROLE_TO_GATE_TYPE.get(task.role)
    if gate_type is None:
        logger.error("spec %s: no gate type for role %r", spec.id, task.role)
        db.update_task_status(task.id, TaskStatus.FAILED)
        return

    gate = _latest_gate_of_type(db, spec.id, gate_type)
    if gate is None or gate.status == GateStatus.PENDING:
        return  # waiting on the human

    if gate.status == GateStatus.APPROVED:
        logger.info("spec %s: task %s (%s) approved, marking done",
                    spec.id, task.id, task.role)
        db.update_task_status(task.id, TaskStatus.DONE)
    elif gate.status == GateStatus.REJECTED:
        _handle_gate_rejection(db, spec, task, gate)
        # If handling left the task parked in BLOCKED_ON_REVIEW (the
        # request_clarification path), retire the rejected gate: leaving it
        # REJECTED re-runs _handle_gate_rejection on it every tick —
        # duplicate supervisor calls and CLARIFICATION gates (each mirrored
        # to Jira) until the transition budget aborts the spec (DEV-122).
        # Rejections that moved the task on (retry paths) keep their gate:
        # _run_implementer reads REJECTED CODE_REVIEW gates for notes.
        fresh = db.get_task(task.id)
        if fresh is not None and fresh.status == TaskStatus.BLOCKED_ON_REVIEW:
            db.cancel_gate(gate.id)


def _handle_gate_rejection(db: Database, spec: Spec, task, gate) -> None:
    """Decide what to do after a human-rejected review gate.

    With SUPERVISOR_ENABLED, asks the supervisor agent; on SupervisorError,
    falls back to the legacy role-keyed branching.
    """
    if not SUPERVISOR_ENABLED:
        _legacy_handle_gate_rejection(db, spec, task, gate)
        return

    ctx = _build_supervisor_context(
        db, spec, task,
        outcome="review_reject",
        reviewer_notes=gate.reviewer_notes,
    )
    try:
        decision = _supervisor.decide(ctx)
    except _supervisor.SupervisorError as e:
        logger.warning("spec %s: supervisor failed (%s); falling back to legacy gate handler",
                       spec.id, e)
        _legacy_handle_gate_rejection(db, spec, task, gate)
        return
    _apply_supervisor_decision(db, spec, task, decision,
                               legacy_feedback=gate.reviewer_notes)


def _legacy_handle_gate_rejection(db: Database, spec: Spec, task, gate) -> None:
    """Handle a rejected review gate for a task."""
    if task.role == "architect":
        # Feed the rejection notes through the channel the architect
        # actually reads. _run_architect only looks for feedback when
        # retry_count > 0, via _latest_architect_feedback — which reads
        # design_review_feedback.md, not CLARIFICATION gates. The old code
        # parked the notes in a synthetic CLARIFICATION gate (a channel only
        # the planner consumes) and never incremented the retry count, so
        # the architect re-ran blind with rejection_notes=None and
        # regenerated ~the same design, gate after gate (DEV-124).
        if gate.reviewer_notes:
            try:
                (db.spec_dir(spec.id) / "design_review_feedback.md").write_text(
                    gate.reviewer_notes)
            except OSError as e:
                logger.warning("spec %s: could not persist design rejection "
                               "notes: %s", spec.id, e)
        db.increment_task_retry(task.id)
        db.update_task_status(task.id, TaskStatus.PENDING)
        logger.info("spec %s: architect design rejected, re-running with "
                    "feedback (retry %d)", spec.id, task.retry_count + 1)

    elif task.role == "implementer":
        impl_task = task
        if impl_task.retry_count < MAX_RETRIES:
            db.increment_task_retry(impl_task.id)
            db.update_task_status(impl_task.id, TaskStatus.PENDING)
            logger.info("spec %s: code rejected by human, retry %d/%d",
                        spec.id, impl_task.retry_count + 1, MAX_RETRIES)
        else:
            # DEV-433: exhaustion here used to fail the spec outright, while
            # the identical exhaustion reached via a failing test run went
            # through the synthesis escape hatch. Whether the accumulated
            # attempts survived depended on who noticed the defect, not on
            # what the defect was — and the gate path carries strictly more
            # information, since it comes with the reviewer's written notes.
            # Route both through _legacy_attempt_retry, which synthesises at
            # MAX_RETRIES and only fails the spec if synthesis fails too.
            reviewer_tasks = db.list_tasks_for_spec_by_role(spec.id, "reviewer")
            reviewer_task = reviewer_tasks[0] if reviewer_tasks else None
            if reviewer_task is None:
                logger.error("spec %s: code rejected, max retries exhausted "
                             "and no reviewer task to synthesise into",
                             spec.id)
                db.update_task_status(impl_task.id, TaskStatus.FAILED)
                db.update_spec_status(spec.id, SpecStatus.FAILED)
            else:
                logger.info("spec %s: code rejected, max retries exhausted — "
                            "attempting synthesis from the rejected attempts",
                            spec.id)
                _legacy_attempt_retry(
                    db, spec, reviewer_task,
                    gate.reviewer_notes or "Rejected at the code review gate.",
                )

    elif task.role == "reviewer":
        # Release rejected — send back to implementer.
        impl_tasks = db.list_tasks_for_spec_by_role(spec.id, "implementer")
        impl_task = impl_tasks[0] if impl_tasks else None
        if impl_task and impl_task.retry_count < MAX_RETRIES:
            if gate.reviewer_notes:
                synth = db.create_gate(
                    spec_id=spec.id,
                    task_id=impl_task.id,
                    gate_type=GateType.CODE_REVIEW,
                    prompt_md="## Release rejection — retry",
                )
                db.respond_to_gate(synth.id, "rejected",
                                   notes=gate.reviewer_notes)
            db.increment_task_retry(impl_task.id)
            db.update_task_status(impl_task.id, TaskStatus.PENDING)
            db.update_task_status(task.id, TaskStatus.PENDING)
            logger.info("spec %s: release rejected, retrying implementer",
                        spec.id)
        else:
            db.update_task_status(task.id, TaskStatus.FAILED)
            db.update_spec_status(spec.id, SpecStatus.FAILED)
            logger.error("spec %s: release rejected, max retries exhausted",
                         spec.id)


def _list_code_artifacts(db: Database, spec_id: str):
    """Return all CODE artifacts for a spec (for feeding to the reviewer)."""
    return db.list_artifacts(spec_id, kind=ArtifactKind.CODE)


def _build_jira_client() -> JiraClient:
    """Construct the right Jira client based on environment configuration.

    If JIRA_URL / JIRA_EMAIL / JIRA_API_TOKEN are all set, use the real
    Atlassian client. Otherwise default to FakeJiraClient — the daemon
    still runs, the sync worker still runs, it just doesn't talk to a
    real Jira instance. This means we can develop and test against the
    fake without needing live credentials.
    """
    url = os.getenv("JIRA_URL", "").strip()
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    project_key = os.getenv("JIRA_PROJECT_KEY", "AUTO").strip()

    if url and email and token:
        try:
            client: JiraClient = AtlassianApiJiraClient(
                url=url, email=email, api_token=token, project_key=project_key,
            )
            logger.info("Jira sync ENABLED (real client, project=%s)",
                        project_key)
            return client
        except Exception as e:
            logger.warning(
                "Jira credentials present but client init failed (%s); "
                "falling back to FakeJiraClient", e,
            )

    logger.info("Jira sync running with FakeJiraClient (no JIRA_URL/EMAIL/"
                "TOKEN configured) — events will not reach a real Jira "
                "instance, but the worker will run normally")
    return FakeJiraClient(project_key=project_key)


def _gate_age_minutes(gate) -> float:
    """Minutes since *gate* was created, or 0.0 if the timestamp is unusable."""
    created = getattr(gate, "created_at", None)
    if created is None:
        return 0.0
    try:
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
        return max(0.0, (now - created).total_seconds() / 60.0)
    except (ValueError, TypeError, AttributeError):
        # An age is a nicety; never let it break the report it decorates.
        return 0.0


def report_open_gates(db: Database, *, startup: bool = False) -> int:
    """Log every gate waiting on a human; return how many there are (DEV-430).

    A spec blocked on review is otherwise indistinguishable from an idle
    daemon: the blocked-on-review branch returns silently every tick, and the
    health endpoint stays green. On DEV-102 that read as "nothing to do" twice
    in one afternoon — once for 18 minutes after a restart, once for 12
    minutes with no restart involved, because the reviewer had no signal that
    a gate had opened.
    """
    try:
        gates = db.list_open_gates()
    except Exception:
        logger.exception("could not list open gates")
        return 0

    if not gates:
        if startup:
            logger.info("no review gates are waiting on a human")
        return 0

    prefix = "waiting on a human at startup" if startup else "still waiting on a human"
    for gate in gates:
        logger.warning("%s: gate %s (%s) on spec %s — %.0f min",
                       prefix, gate.id,
                       getattr(gate.gate_type, "value", gate.gate_type),
                       gate.spec_id, _gate_age_minutes(gate))
    return len(gates)


def main() -> int:
    logger.info("orchestrator daemon starting (poll=%.1fs)", POLL_INTERVAL)
    db = Database()
    logger.info("task store: %s", db.db_path)
    logger.info("workspace:  %s", db.workspace_root)

    # Single-instance guard (DEV-142): the DB's one-writer story was an
    # assumption, not enforced — a manual debug run beside the systemd unit
    # gave two pollers double-processing every PENDING task. The flock dies
    # with the process, so a crashed daemon never wedges the lock.
    import fcntl
    lock_path = Path(db.db_path).with_suffix(".lock")
    # "a", never "w": mode "w" truncates at open, before the flock decides who
    # owns the file — a refused second start still wiped the live daemon's
    # recorded pid, destroying the one diagnostic the file exists to carry.
    lock_file = open(lock_path, "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error(
            "another orchestrator daemon already holds %s — refusing to "
            "start a second poller (stop the systemd unit first: "
            "systemctl stop coding-model-orchestrator)", lock_path,
        )
        return 1
    lock_file.truncate(0)
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()

    # Retention sweep (DEV-144): heartbeat rows land every 60s forever and
    # nothing else deletes from events.
    pruned = db.prune_daemon_ticks()
    if pruned:
        logger.info("pruned %d DAEMON_TICK heartbeat events (>14 days old)", pruned)

    # Sandbox pre-flight (DEV-155): a degraded sandbox used to surface as
    # one log line per test run, buried mid-spec. Say it once, at startup,
    # where an operator actually looks — and refuse outright if asked to.
    sandbox_ok, sandbox_detail = test_runner.seccomp_preflight()
    if sandbox_ok:
        logger.info("test sandbox pre-flight: %s", sandbox_detail)
    else:
        logger.warning("TEST SANDBOX DEGRADED: %s", sandbox_detail)
        if os.getenv("CODING_MODEL_REQUIRE_SECCOMP", "0") == "1":
            logger.error(
                "CODING_MODEL_REQUIRE_SECCOMP=1 — refusing to start with a "
                "degraded test sandbox")
            return 1

    # Phase b pre-flight: if the operator flipped the flag, surface up
    # front whether each configured provider will actually fire — otherwise
    # a missing key only shows up as a runtime warning per spec.
    if adversarial.ADVERSARIAL_TESTS_ENABLED:
        providers = adversarial._resolve_providers()
        provider_summary = ", ".join(
            f"{p}={adversarial._provider_model(p)}" for p in providers
        )
        adv_ok, adv_reason = adversarial.adversarial_tests_available()
        if adv_ok:
            logger.info(
                "phase-b adversarial test generation ENABLED (providers=[%s], "
                "max_tokens=%d, timeout=%.0fs)",
                provider_summary,
                adversarial.ADVERSARIAL_MAX_TOKENS,
                adversarial.ADVERSARIAL_TIMEOUT,
            )
        else:
            logger.warning(
                "phase-b adversarial test generation flag is ON but a "
                "configured provider is unavailable (%s) — providers=[%s]. "
                "Fix the env or change AUTONOMOUS_ADVERSARIAL_PROVIDER to "
                "silence this.",
                adv_reason, provider_summary,
            )

    # Spin up the Jira sync worker on its own thread. It shares the same
    # Database instance (SQLite WAL is thread-safe) and runs independently
    # of the main planner loop, so a Jira outage never blocks planning.
    jira_client = _build_jira_client()
    jira_sync = JiraSync(db, jira_client)
    jira_sync.start()

    flag = _shutdown_flag
    flag.install_handlers()

    HEARTBEAT_INTERVAL = 60.0

    def _heartbeat_loop():
        # DEV-141: heartbeats used to ride the tick thread, so they went
        # silent for the entire length of a blocking agent call (single
        # calls run up to 45 min) — the liveness signal died exactly when
        # the daemon was busiest. A side thread keeps it honest; SQLite WAL
        # + thread-local connections make the cross-thread write safe.
        while not flag.set:
            try:
                db.record_event(EventKind.DAEMON_TICK,
                                payload={"poll_interval": POLL_INTERVAL})
            except Exception:
                logger.exception("heartbeat write failed")
            slept = 0.0
            while slept < HEARTBEAT_INTERVAL and not flag.set:
                time.sleep(0.5)
                slept += 0.5

    threading.Thread(target=_heartbeat_loop, name="daemon-heartbeat",
                     daemon=True).start()

    scheduler = SpecScheduler()
    logger.info("spec worker pool: %d workers", SPEC_WORKERS)

    # Say immediately what we are waiting for. A restart that logs only its
    # banner and then goes quiet looks identical to a restart with no work.
    report_open_gates(db, startup=True)
    next_gate_report = time.monotonic() + GATE_REPORT_INTERVAL

    try:
        while not flag.set:
            try:
                tick(db, scheduler)
            except Exception:
                logger.exception("tick failed; continuing")

            # Aged reminder, not per-tick: a 5s poll would drown the log.
            if time.monotonic() >= next_gate_report:
                report_open_gates(db)
                next_gate_report = time.monotonic() + GATE_REPORT_INTERVAL

            # Sleep in small chunks so SIGTERM is responsive.
            slept = 0.0
            while slept < POLL_INTERVAL and not flag.set:
                time.sleep(min(0.5, POLL_INTERVAL - slept))
                slept += 0.5
    finally:
        logger.info("waiting for in-flight spec passes...")
        scheduler.drain()
        logger.info("stopping jira-sync worker...")
        jira_sync.stop()

    logger.info("orchestrator daemon stopped")
    db.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
