"""Deliver a completed spec's artifacts to a branch of its target repo.

DEV-535: the pipeline's terminal state used to be a patch in a scratch dir —
four fully-approved runs (DEV-202, DEV-208, Centipede slice 1, run 13) ended
with Jira reporting Done while the target repository stayed byte-identical.
This module is the ticket's option 1, extended per the operator's manual
precedent of 2026-08-11: on a fully approved run, clone the target repo, apply
the CODE artifacts to a `pipeline/<spec_id>` branch, commit with provenance,
and PUSH. The operator reviews and merges; the pipeline never touches the
default branch.

Fail-open by design: a delivery failure is reported loudly (delivery_report.md
plus an event) but never un-DONEs a spec — the run's verification stands
regardless of whether the last hop succeeded.

Remotes are configured with a single env var, loaded from the daemon's .env:

    AUTONOMOUS_DELIVERY_REMOTES="electric-sheep=git@github.com:me/ElectricSheep.git, centipede=git@github.com:me/Centipede.git"

A spec whose test_strategy names no repo (greenfield runs), or names a repo
with no configured remote, is skipped with an honest report naming the
artifact paths instead — the DEV-535 failure mode was silence, not the skip.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("orchestrator")

BRANCH_PREFIX = "pipeline/"


def delivery_remotes() -> dict[str, str]:
    """Parse AUTONOMOUS_DELIVERY_REMOTES into {repo_name: git_url}."""
    raw = os.getenv("AUTONOMOUS_DELIVERY_REMOTES", "")
    out: dict[str, str] = {}
    for pair in re.split(r"[,\s]+", raw.strip()):
        if "=" in pair:
            name, url = pair.split("=", 1)
            if name.strip() and url.strip():
                out[name.strip()] = url.strip()
    return out


@dataclass(frozen=True)
class DeliveryResult:
    status: str            # 'pushed' | 'skipped' | 'failed'
    detail: str
    branch: Optional[str] = None


def _git(cwd, *args, timeout: int = 120):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def deliver_spec(spec_id: str, spec_title: str, spec_dir: Path,
                 code_paths: "list[str]", repo_name: Optional[str],
                 protected_paths: "list[str]") -> DeliveryResult:
    """Apply the spec's code artifacts to a pipeline branch and push it.

    `code_paths` are repo-relative artifact paths; only files that exist in
    the spec workspace and are not protected are delivered. Protected paths
    are excluded unconditionally — the runner discards writes to them during
    the run, so delivering one would ship content no test ever saw.
    """
    if not repo_name:
        return DeliveryResult(
            "skipped",
            "spec names no target repo (greenfield or repo-less strategy); "
            f"artifacts remain in the spec workspace: {spec_dir}")
    url = delivery_remotes().get(repo_name)
    if not url:
        return DeliveryResult(
            "skipped",
            f"no delivery remote configured for repo '{repo_name}' — set "
            f'AUTONOMOUS_DELIVERY_REMOTES="{repo_name}=<git-url>" and the '
            f"artifacts in {spec_dir} can be re-delivered")

    protected = set(protected_paths or [])
    deliverable = []
    for rel in dict.fromkeys(code_paths):  # dedupe, keep order
        if rel in protected:
            continue
        if (spec_dir / rel).is_file():
            deliverable.append(rel)
    if not deliverable:
        return DeliveryResult(
            "skipped", "no deliverable code artifact exists on disk "
                       "(all protected, or paths missing from the workspace)")

    branch = f"{BRANCH_PREFIX}{spec_id}"
    tmp = tempfile.mkdtemp(prefix="delivery-")
    try:
        clone = _git(tmp, "clone", "--depth", "1", url, "repo", timeout=300)
        if clone.returncode != 0:
            return DeliveryResult(
                "failed", f"clone of {url} failed: {clone.stderr.strip()[:400]}")
        repo = Path(tmp) / "repo"
        _git(repo, "checkout", "-b", branch)
        for rel in deliverable:
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(spec_dir / rel, dst)
        _git(repo, "add", "-A")
        if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
            return DeliveryResult(
                "skipped", "the target repo's default branch already contains "
                           "every artifact byte-for-byte — nothing to deliver")
        msg = (f"pipeline: {spec_title} ({spec_id})\n\n"
               f"Delivered by the autonomous pipeline after release approval "
               f"(DEV-535): {len(deliverable)} file(s), protected paths "
               f"excluded. This branch is pipeline-owned and force-pushed on "
               f"re-delivery; review and merge, never build on it.")
        commit = _git(repo, "-c", "user.name=coding-model-pipeline",
                      "-c", "user.email=pipeline@zooshly.invalid",
                      "commit", "-m", msg)
        if commit.returncode != 0:
            return DeliveryResult(
                "failed", f"commit failed: {commit.stderr.strip()[:400]}")
        push = _git(repo, "push", "--force", "origin", branch, timeout=300)
        if push.returncode != 0:
            return DeliveryResult(
                "failed", f"push to {url} failed: {push.stderr.strip()[:400]}")
        return DeliveryResult(
            "pushed",
            f"{len(deliverable)} file(s) committed to {branch} of {url}",
            branch=branch)
    except Exception as e:  # never let delivery take down the tick
        return DeliveryResult("failed", f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
