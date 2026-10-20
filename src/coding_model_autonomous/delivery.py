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

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .models import EventKind
from .workspace import is_git_control_path, read_bytes_nofollow, symlink_in_path

logger = logging.getLogger("orchestrator")

BRANCH_PREFIX = "pipeline/"

TESTED_MANIFEST = "tested_manifest.json"
DELIVERY_BASE = "delivery_base.json"

# DEV-756: the names a test file declares, so a delivery that would REMOVE a
# test the default branch has is refused rather than pushed. Both Swift
# styles (XCTest `func testX`, Swift Testing `@Test func x`) and pytest.
_TEST_NAME_RES = (
    re.compile(r"\bfunc\s+(test\w*)\s*\("),
    re.compile(r"@Test\b[^\n]*?\bfunc\s+(\w+)\s*\("),
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.MULTILINE),
)


def test_names(source: str) -> set:
    """Test function names declared in *source* (comment lines excluded)."""
    text = "\n".join(ln for ln in source.splitlines()
                     if not ln.lstrip().startswith(("//", "#")))
    names: set = set()
    for pat in _TEST_NAME_RES:
        names.update(pat.findall(text))
    return names


@dataclass(frozen=True)
class BaseAssessment:
    """What delivery found comparing the artifact with the default branch."""
    refusal: "str | None"
    # Tests renamed on a current base: allowed, and shown on the delivery
    # (DEV-810). One line per file.
    renames: "tuple[str, ...]" = ()


def assess_base(repo: Path, deliverable: "list[str]", spec_dir: Path,
                base_files: "dict[str, str] | None",
                base_is_current: "bool | None" = None) -> BaseAssessment:
    """Judge a delivery against the freshly cloned default branch (DEV-756).

    DEV-810: a test name that vanishes from a file whose test count holds or
    grows is a probable RENAME, not a deletion — run 57 renamed
    `adapterMapsEveryDrawableKindAndDropsShot` because the slice made the shot
    reach the screen, and was refused as a "stale base" on a base that had not
    moved. So the diagnosis now follows the evidence:

      * "stale base" only when the base is known to differ from main;
      * a rename on a CURRENT base is the artifact's own reviewed change and is
        delivered, with both name lists on the record;
      * a deletion (names gone AND the count fell) is refused on any base;
      * a rename on a stale or unrecorded base is refused, named as a probable
        rename, because decay and intent cannot be told apart there.
    """
    deletions: list[str] = []
    renames: list[str] = []
    moved: list[str] = []
    for rel in deliverable:
        current = repo / rel
        if not current.is_file():
            continue          # new file on main: nothing to regress
        main_src = current.read_text(errors="replace")
        ours = (spec_dir / rel).read_text(errors="replace")
        main_names, our_names = test_names(main_src), test_names(ours)
        lost = sorted(main_names - our_names)
        if lost:
            gained = sorted(our_names - main_names)
            counts = f"test count {len(main_names)} → {len(our_names)}"
            if len(our_names) >= len(main_names):
                renames.append(f"  {rel}: probable rename ({counts}) — gone: "
                               + ", ".join(lost[:8])
                               + (" …" if len(lost) > 8 else "")
                               + "; new: " + (", ".join(gained[:8]) or "none"))
            else:
                deletions.append(f"  {rel}: removes {len(lost)} test(s) main has "
                                 f"({counts}): " + ", ".join(lost[:8])
                                 + (" …" if len(lost) > 8 else ""))
        base_src = (base_files or {}).get(rel)
        if base_src is not None and main_src != base_src and main_src != ours:
            moved.append(f"  {rel}: main changed since the base this artifact "
                         "was written against")
    if not deletions and not moved and (not renames or base_is_current):
        return BaseAssessment(None, tuple(renames))

    if base_is_current:
        lines = ["REFUSED — the artifact removes tests the default branch has; "
                 "nothing was pushed. The base is current, so this is the "
                 "artifact's own change, not decay (DEV-756/DEV-810)."]
    elif base_is_current is False or moved:
        lines = ["REFUSED — stale base; nothing was pushed (DEV-756)."]
    else:
        lines = ["REFUSED — base not recorded, so decay cannot be ruled out; "
                 "nothing was pushed (DEV-756/DEV-810)."]
    if deletions:
        lines.append("Delivering would REMOVE tests the default branch has:")
        lines += deletions
    if renames:
        lines.append("Test names that would disappear while the file keeps as "
                     "many tests (probable renames — check them):")
        lines += renames
    if moved:
        lines.append("The default branch moved under these files since base_ref, "
                     "and the artifact would overwrite that movement:")
        lines += moved
    if base_is_current:
        lines.append("Re-running against main would reproduce this. If the "
                     "removal is intended, review the diff and deliver by hand; "
                     "otherwise reject the artifact. The workspace copy is "
                     "unchanged.")
    elif base_is_current is False or moved:
        lines.append("Re-run the spec against current main, or rebase the "
                     "artifact by hand; the workspace copy is unchanged.")
    else:
        lines.append("If the names above are intended renames, deliver by hand "
                     "after reviewing the diff; the workspace copy is unchanged.")
    return BaseAssessment("\n".join(lines), tuple(renames))


def _base_from_context(spec_dir: Path) -> "tuple[str | None, dict[str, str]]":
    """(base sha, base file contents) from the spec's persisted context, or
    (None, {}) when there is none — pre-DEV-701 workspaces and tests."""
    try:
        from .context import SpecContext
        ctx = SpecContext.load(spec_dir)
    except Exception:
        return None, {}
    if ctx is None:
        return None, {}
    sha = None
    if isinstance(ctx.ref_state, dict):
        sha = ctx.ref_state.get("local_sha") or None
    files = dict(ctx.editable)
    files.update(ctx.protected)
    return sha, files


def verify_tested_manifest(spec_dir: Path, deliverable: "list[str]",
                           recorded: "dict | None" = None,
                           shipped: "dict[str, str] | None" = None,
                           ) -> "tuple[bool, str]":
    """Refuse to ship bytes the build check never verified (DEV-602 split C).

    Split B records what a passing pre-gate build check verified (workspace-
    relative path → sha256 of on-disk bytes) twice: in `tested_manifest.json`
    in the workspace, and in that check's event payload. Run 17 shipped files
    that were not the files the check had tested; delivery compared nothing.
    This is the compare step, factored out so tests exercise it without a git
    remote.

    *recorded* is the event's copy, and when the caller has it, it is the one
    compared against: the workspace is writable by the code under test, so the
    file there can be rewritten or deleted to match swapped artifacts, and the
    database cannot. The workspace file must still be present and a regular
    file — its absence after a check recorded one means something modified
    the workspace behind the pipeline, which is itself a reason not to ship.

    With no *recorded* copy (no check ever recorded one, or a caller without
    a database) the workspace file is the record: ABSENT is (True, "") and
    delivery proceeds exactly as it did before manifests existed; present, it
    is compared.

    Returns (ok, detail). Any hash mismatch, manifest path missing from the
    shipping set, or shipped file absent from the manifest is a divergence,
    and the detail lists every divergent path with its tested and shipped
    hashes (or "missing"). An unreadable manifest also refuses — it is
    corruption of exactly the record this check depends on. Symlinks are
    never followed: neither the manifest nor an artifact, since a link's
    target is not what the check hashed.

    *shipped* (path → sha256) lets a caller that has already read the bytes
    it is about to ship have exactly those bytes compared.
    """
    manifest_path = spec_dir / TESTED_MANIFEST
    if os.path.islink(manifest_path):
        return False, (
            f"REFUSED — {TESTED_MANIFEST} is a symbolic link; it is the "
            f"record of what the build check verified and is never read "
            f"through a link, so nothing was pushed (DEV-602)")
    if recorded is not None:
        if not manifest_path.is_file():
            return False, (
                f"REFUSED — {TESTED_MANIFEST} is missing, but the build check "
                f"recorded one for this spec; the workspace was modified "
                f"after the check, so nothing was pushed (DEV-602)")
        tested, source = recorded, "the build check's recorded hashes"
    else:
        if not manifest_path.is_file():
            return True, ""
        try:
            tested = json.loads(read_bytes_nofollow(manifest_path))
            if not isinstance(tested, dict):
                raise ValueError(
                    f"manifest is {type(tested).__name__}, not a dict")
        except Exception as e:
            return False, (
                f"REFUSED — {TESTED_MANIFEST} exists but cannot be read "
                f"({e}); it is the record of what the build check verified, "
                f"so nothing was pushed (DEV-602)")
        source = TESTED_MANIFEST

    if shipped is None:
        shipped = {}
        for rel in deliverable:
            link = symlink_in_path(spec_dir, rel)
            if link is not None:
                return False, (
                    f"REFUSED — {rel}: `{link}` is a symbolic link in the "
                    f"workspace; nothing was pushed")
            f = spec_dir / rel
            if f.is_file():
                shipped[rel] = hashlib.sha256(
                    read_bytes_nofollow(f)).hexdigest()

    divergent: list[str] = []
    for rel in sorted(set(tested) | set(shipped)):
        t, s = tested.get(rel), shipped.get(rel)
        if t != s:
            divergent.append(f"  {rel}: tested {t or 'missing'} "
                             f"shipped {s or 'missing'}")
    if divergent:
        return False, (
            "REFUSED — tested-manifest divergence; nothing was pushed.\n"
            "The build check verified different bytes than this delivery "
            "would ship (DEV-602):\n" + "\n".join(divergent))
    return True, f"{len(shipped)} file(s) verified against {source}"


def unsafe_artifact_reason(spec_dir: Path, rel: str) -> "str | None":
    """Why *rel* must not be copied out of the workspace, or None.

    An artifact path is chosen by a model and the file at it can be replaced
    by the code under test (the sandbox binds the workspace read-write), so
    both are checked at the last moment before they leave the machine:

      * the path must be a plain relative one — no absolute path, no `..`;
      * no component may be git's control directory: a `.git/config` copied
        into the clone is configuration git then acts on (`core.fsmonitor`
        names a command run by the very next `git add`);
      * the source, and every directory above it inside the workspace, must
        not be a symlink — delivery would otherwise push whatever the link
        points at (a deploy key, /proc/self/environ).

    `.gitmodules`, `.gitattributes` and `.gitignore` are deliberately NOT
    refused. They are ordinary tracked content that a legitimate change may
    need to touch, and none of them executes anything in the delivery clone:
    a filter or diff driver named in `.gitattributes` only runs if a
    `filter.<name>.*` command is configured, and configuration lives in
    `.git/config` (refused above) or the daemon user's own config (trusted);
    `.gitmodules` cannot carry an `update = !command` (git only honours that
    from config) and nothing delivery runs — add, commit, push — fetches a
    submodule. What they say reaches the operator as a diff on the pipeline
    branch, to be reviewed like the code beside it.
    """
    posix = rel.replace("\\", "/")
    if posix.startswith("/") or re.match(r"^[A-Za-z]:", posix):
        return "absolute path"
    if ".." in posix.split("/"):
        return "path contains `..`"
    if is_git_control_path(rel):
        return "path is inside git's control directory"
    link = symlink_in_path(spec_dir, rel)
    if link is not None:
        return f"`{link}` is a symbolic link in the workspace"
    return None


def _destination_refusal(repo: Path, rel: str) -> "str | None":
    """Why writing *rel* into the clone would land somewhere else, or None.

    The clone is the target repository's content, which can itself hold
    tracked symlinks: a link `docs -> ../..` or `meta -> .git` turns an
    innocent-looking `docs/x` or `meta/config` into a write outside the
    clone or into its configuration. Resolved, the destination must still be
    inside the clone and outside `.git`.
    """
    root = repo.resolve()
    dst = (repo / rel).resolve()
    if not dst.is_relative_to(root):
        return "resolves outside the clone"
    if is_git_control_path(dst.relative_to(root).as_posix()):
        return "resolves into the clone's git control directory"
    return None


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


# ── Delivery owed ────────────────────────────────────────────────────────────
#
# A push can take minutes and the daemon's shutdown grace is seconds, so a
# delivery can be cut off with nothing to show for it — and a DONE spec is
# never walked by the state machine again. Whether one is still owed is
# therefore derived from the spec's own events, which survive the restart:
#
#   delivery_requested   someone asked: the release (written before the DONE
#                        transition) or the operator's `redeliver`
#   delivery_attempt     the daemon started one
#   delivery             it reached an outcome: pushed, skipped or failed
#
# Delivery is owed while a request or an attempt is newer than the last
# outcome. A failure is an outcome — it has a report, and retrying it would
# loop — so only an attempt that left NO outcome is retried, and the attempts
# since the last outcome are what the cap counts. All three ride AGENT_RAN
# with `model_call: False`, the convention for records of what the daemon
# itself did.
REQUESTED_ROLE = "delivery_requested"
ATTEMPT_ROLE = "delivery_attempt"
OUTCOME_ROLE = "delivery"


@dataclass(frozen=True)
class DeliveryDebt:
    owed: bool
    attempts: int          # started since the last outcome


def delivery_debt(db: Any, spec_id: str) -> DeliveryDebt:
    """What the spec's events say about delivery since its last outcome."""
    owed, attempts = False, 0
    # Newest first; every row is read (a newest-N window would lose an old
    # request behind a long tail of unrelated records).
    for ev in db.list_events_by_kind(spec_id=spec_id, kind=EventKind.AGENT_RAN,
                                     limit=-1):
        role = (ev.payload or {}).get("role")
        if role == OUTCOME_ROLE:
            break
        if role == ATTEMPT_ROLE:
            owed, attempts = True, attempts + 1
        elif role == REQUESTED_ROLE:
            owed = True
    return DeliveryDebt(owed, attempts)


def request_delivery(db: Any, spec_id: str, trigger: str) -> bool:
    """Record that delivery is owed for *spec_id*. False, and nothing
    written, when it already is — one debt is delivered once."""
    if delivery_debt(db, spec_id).owed:
        return False
    db.record_event(EventKind.AGENT_RAN, spec_id=spec_id,
                    payload={"role": REQUESTED_ROLE, "model_call": False,
                             "trigger": trigger})
    return True


def _delivery_key(repo_name: Optional[str]) -> str:
    """Deploy-key path for *repo_name* (DEV-574).

    GitHub binds a deploy key to exactly one repository, so each remote gets
    its own: AUTONOMOUS_DELIVERY_SSH_KEY_<REPO> (upper-cased, non-alnum → _),
    falling back to AUTONOMOUS_DELIVERY_SSH_KEY. Empty when unconfigured —
    _git then runs with ambient SSH, which works from an interactive shell.

    Keep these paths OUTSIDE the config dir the unit marks Inaccessible, or
    the daemon cannot read its own credential (DEV-597).
    """
    specific = ""
    if repo_name:
        var = "AUTONOMOUS_DELIVERY_SSH_KEY_" + re.sub(
            r"[^A-Za-z0-9]", "_", repo_name).upper()
        specific = os.getenv(var, "").strip()
    return specific or os.getenv("AUTONOMOUS_DELIVERY_SSH_KEY", "").strip()


def _known_hosts_for(key_path: str) -> str:
    """The pinned known_hosts that ships beside the deploy key (DEV-597)."""
    return str(Path(key_path).parent / "known_hosts")


def _check_delivery_credentials(key: str) -> Optional[str]:
    """Validate a *configured* credential before any git command runs.

    DEV-597: the daemon's unit sandbox (InaccessiblePaths) can hide both the
    deploy key and ~/.ssh, and the old behavior — silently dropping
    GIT_SSH_COMMAND when the key file "didn't exist" — swapped in the user's
    ambient identity, which then failed on host-key verification with no hint
    of the real cause. A configured credential that cannot be read is a
    delivery failure that names the path; ambient ssh is never a fallback.

    An EMPTY key stays permitted: that is the deliberate interactive-shell
    path (operator reruns with their own agent).
    """
    if not key:
        return None
    kp = os.path.expanduser(key)
    if not (os.path.isfile(kp) and os.access(kp, os.R_OK)):
        return (f"delivery key {kp} is not readable in this context; if this "
                f"is the daemon, the unit sandbox (InaccessiblePaths) is "
                f"probably hiding it — refusing to fall back to ambient ssh "
                f"(DEV-597)")
    kh = _known_hosts_for(kp)
    if not (os.path.isfile(kh) and os.access(kh, os.R_OK)):
        return (f"pinned known_hosts {kh} is missing or unreadable; it must "
                f"ship beside the deploy key so ssh never needs ~/.ssh "
                f"(DEV-597)")
    return None


# Every git command delivery runs gets these, so nothing in the tree it is
# operating on can make git run a program. `core.fsmonitor` names a command
# git executes on any index refresh (status, add, diff), and hooks fire on
# commit and push; both are read from the repository's own config, which is
# exactly the file a planted `.git/config` would be. The path checks in
# deliver_spec keep such a file from being written at all — this is the second
# lock, for the case where one is missed. A command-line -c outranks every
# config file. (`core.sshCommand` needs no entry: GIT_SSH_COMMAND, set below
# whenever a key is configured, takes precedence over it.)
_GIT_HARDENING = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")


def _git(cwd, *args, timeout: int = 120, key: str = ""):
    env = os.environ.copy()
    # DEV-574: the daemon's systemd context has no SSH agent, and the user
    # key is passphrase-protected — so delivery authenticated exactly when a
    # human's shell ran it and failed exactly when the pipeline did. A
    # dedicated passphrase-less deploy key (write access scoped to the one
    # delivery remote) restores daemon-context pushes without granting the
    # daemon the user's ambient identity.
    # DEV-597: the pinned known_hosts beside the key keeps host verification
    # out of ~/.ssh, which the unit sandbox hides. Callers must have passed
    # the key through _check_delivery_credentials first.
    if key:
        kp = os.path.expanduser(key)
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {kp} -o IdentitiesOnly=yes -o BatchMode=yes "
            f"-o UserKnownHostsFile={_known_hosts_for(kp)} "
            f"-o StrictHostKeyChecking=yes")
    return subprocess.run(["git", *_GIT_HARDENING, *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _err_tail(proc, limit: int = 400) -> str:
    """The actionable end of git's stderr (DEV-574): the first line is a
    banner ('Cloning into ...'); the auth/network diagnosis is at the tail."""
    return (proc.stderr or "").strip()[-limit:]


def deliver_spec(spec_id: str, spec_title: str, spec_dir: Path,
                 code_paths: "list[str]", repo_name: Optional[str],
                 protected_paths: "list[str]",
                 tested_hashes: "dict | None" = None) -> DeliveryResult:
    """Apply the spec's code artifacts to a pipeline branch and push it.

    `code_paths` are repo-relative artifact paths; only files that exist in
    the spec workspace and are not protected are delivered. Protected paths
    are excluded unconditionally — the runner discards writes to them during
    the run, so delivering one would ship content no test ever saw.

    `tested_hashes` is the database's copy of what the last passing build
    check verified (see verify_tested_manifest); None when no check recorded
    one.
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
            f'AUTONOMOUS_DELIVERY_REMOTES="{repo_name}=<git-url>" in the '
            f"orchestrator's environment, restart it, then run "
            f"`coding-model-autonomous redeliver {spec_id}`; the artifacts "
            f"remain in {spec_dir}")

    protected = set(protected_paths or [])
    deliverable = []
    unsafe: list[str] = []
    for rel in dict.fromkeys(code_paths):  # dedupe, keep order
        if rel in protected:
            continue
        why = unsafe_artifact_reason(spec_dir, rel)
        if why:
            unsafe.append(f"  {rel}: {why}")
        elif (spec_dir / rel).is_file():
            deliverable.append(rel)
    if unsafe:
        # One bad path refuses the whole delivery: it means either a model
        # chose a path no deliverable has, or the workspace was rewritten
        # behind the pipeline — neither is a state to ship the rest from.
        return DeliveryResult(
            "failed",
            "REFUSED — unsafe artifact path; nothing was copied and nothing "
            "was pushed.\n" + "\n".join(unsafe) + "\nThe workspace copy is "
            f"unchanged: {spec_dir}")
    if not deliverable:
        return DeliveryResult(
            "skipped", "no deliverable code artifact exists on disk "
                       "(all protected, or paths missing from the workspace)")

    # DEV-602 split C: verify BEFORE credentials or clone — the refusal
    # needs no network, and a divergent set must never get as far as a
    # remote. verified_note is empty when no manifest exists (pre-split-B
    # workspaces keep today's behavior and today's report, byte-identical).
    # The bytes are read ONCE, here, without following a link: what is hashed
    # against the manifest is then exactly what is written into the clone.
    payloads: dict[str, bytes] = {}
    for rel in deliverable:
        try:
            payloads[rel] = read_bytes_nofollow(spec_dir / rel)
        except OSError as e:
            return DeliveryResult(
                "failed", f"REFUSED — could not read {rel} from the "
                          f"workspace ({e}); nothing was pushed")
    verified_ok, verified_note = verify_tested_manifest(
        spec_dir, deliverable, tested_hashes,
        shipped={rel: hashlib.sha256(data).hexdigest()
                 for rel, data in payloads.items()})
    if not verified_ok:
        return DeliveryResult("failed", verified_note)

    branch = f"{BRANCH_PREFIX}{spec_id}"
    key = _delivery_key(repo_name)
    cred_err = _check_delivery_credentials(key)
    if cred_err:
        return DeliveryResult("failed", cred_err)
    tmp = tempfile.mkdtemp(prefix="delivery-")
    try:
        clone = _git(tmp, "clone", "--depth", "1", url, "repo", timeout=300,
                     key=key)
        if clone.returncode != 0:
            return DeliveryResult(
                "failed", f"clone of {url} failed: {_err_tail(clone)}")
        repo = Path(tmp) / "repo"
        # Not checked: an empty remote has no HEAD to parse and is still a
        # legitimate delivery target ("unknown" then says so in the report).
        head_res = _git(repo, "rev-parse", "HEAD")
        head = (getattr(head_res, "stdout", "") or "").strip() or "unknown"
        base_sha, base_files = _base_from_context(spec_dir)
        # DEV-756: refuse a snapshot that has decayed into a deletion, and
        # record the base so the check is computable after the fact.
        base_is_current = (base_sha == head) if base_sha else None
        assessment = assess_base(repo, deliverable, spec_dir, base_files,
                                 base_is_current)
        stale = assessment.refusal
        try:
            (spec_dir / DELIVERY_BASE).write_text(json.dumps({
                "branch": branch, "base_sha": base_sha,
                "default_branch_sha_at_delivery": head,
                "base_is_current": base_is_current,
                "files": deliverable, "refused": bool(stale),
                "renamed_tests": [r.strip() for r in assessment.renames],
            }, indent=2) + "\n")
        except OSError as e:
            logger.warning("could not write %s: %s", DELIVERY_BASE, e)
        if stale:
            return DeliveryResult("failed", stale)
        checkout = _git(repo, "checkout", "-b", branch)
        if checkout.returncode != 0:
            return DeliveryResult(
                "failed", f"creating branch {branch} failed: "
                          f"{_err_tail(checkout)}")
        # Every destination is judged before the first byte is written, so a
        # refusal leaves the clone untouched.
        misplaced = [f"  {rel}: {why}" for rel in deliverable
                     if (why := _destination_refusal(repo, rel))]
        if misplaced:
            return DeliveryResult(
                "failed",
                "REFUSED — an artifact's destination is not a plain file "
                "inside the clone; nothing was copied and nothing was "
                "pushed.\n" + "\n".join(misplaced))
        for rel in deliverable:
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(payloads[rel])
        add = _git(repo, "add", "-A")
        if add.returncode != 0:
            return DeliveryResult(
                "failed", f"staging the artifacts failed: {_err_tail(add)}")
        # `diff --quiet` answers with its exit status: 0 nothing staged, 1
        # something staged. Anything else is git failing, which must not be
        # read as either answer.
        staged = _git(repo, "diff", "--cached", "--quiet")
        if staged.returncode not in (0, 1):
            return DeliveryResult(
                "failed", f"could not compare the artifacts with the default "
                          f"branch: {_err_tail(staged)}")
        if staged.returncode == 0:
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
                "failed", f"commit failed: {_err_tail(commit)}")
        push = _git(repo, "push", "--force", "origin", branch, timeout=300,
                    key=key)
        if push.returncode != 0:
            return DeliveryResult(
                "failed", f"push to {url} failed: {_err_tail(push)}")
        detail = f"{len(deliverable)} file(s) committed to {branch} of {url}"
        if verified_note:
            detail += f" — {verified_note}"
        detail += (f"; cut from default branch {head[:12]}"
                   + (f", base {base_sha[:12]}"
                      + (" (current)" if base_sha == head else " (main moved; no delivered file affected)")
                      if base_sha else ""))
        if assessment.renames:
            detail += ("; tests renamed on a current base (DEV-810):\n"
                       + "\n".join(assessment.renames))
        return DeliveryResult("pushed", detail, branch=branch)
    except Exception as e:  # never let delivery take down the tick
        return DeliveryResult("failed", f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
