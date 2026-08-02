# DEV-400 — Git as transport and source of truth for spec attempts

**Status:** proposed (design for review — no code changes in this doc's branch)
**Author:** drafted 2026-08-02 against `main` @ `486255a1`
**Ticket:** DEV-400. Motivating defects: DEV-399, DEV-196, DEV-393, DEV-391.

## Decision in one paragraph

Spec attempts move from JSON `patch_files` payloads to branches on the local
git server (`zooshly:/srv/private/git`), in a dedicated ref namespace
`refs/auto/spec/<spec_id>/attempt-<n>`, every attempt cut from one pinned
`base_sha` recorded on the spec. The orchestrator is the only writer; the Mac
runner becomes fetch-only; humans never see the namespace unless they ask for
it (git clients only fetch `refs/heads/*` by default). Synthesis reads
`git diff base_sha..attempt-<n>` per attempt instead of concatenated file
bodies. The runner's existing worktree flow keeps everything but the
patch-writing step, which it deletes.

## Current state (what the code actually does today)

* `test_runner._collect_patch_files` recursively dumps the whole `spec_dir`
  as `{path, content}` UTF-8 JSON and POSTs it to the runner
  (`_run_mac_runner_tests`, `mac_runner/server.py:RunTestsRequest`).
  Binary files are unsupported; deletions are inexpressible — a patch can
  only add or overwrite.
* `mac_runner/workspace.py` already creates a **git worktree** from
  `base_ref` (default `"HEAD"` — floating, so two retries of one spec can
  build against different bases), then writes the JSON blobs over it.
  `_write_patch_files` carries its own path-escape jail; `integration.py`
  (DEV-399) shells out to `git ls-files` to reconstruct whether the patch
  is related to the real tree. Git is present and being bypassed at the
  last step.
* Retries snapshot the whole `spec_dir` into `retry_history/retry_<n>/`
  (`retry_policy._snapshot_retry`), and synthesis
  (`executor.build_synthesis_message`) pastes **full file bodies of every
  attempt** into one prompt — the observed ~195k-token, 478-second-prefill
  worst case in DEV-393. The DEV-393 scheduler (landed, `75758ec`) stops
  that call from freezing other specs, but the prompt itself is still
  unbounded; this design is what shrinks it.

## Target shape

### Refs and base pinning

* Namespace: `refs/auto/spec/<spec_id>/attempt-<n>`, `<n>` = the retry
  index the DB already tracks. Refs are **create-only**: an attempt is
  written once and never force-pushed or rewritten.
* `base_sha` is resolved **once**, at spec bootstrap, from the repo's
  default branch on zooshly, and stored on the spec row. Every attempt
  branches from it. This replaces the floating `base_ref: HEAD` and is a
  correctness fix on its own.
* Each role commits its own work on the attempt branch (architect design
  notes, implementer sources, reviewer annotations), with trailers:
  `Spec: <spec_id>`, `Attempt: <n>`, `Role: <role>`, `Agent: <agent>` —
  handoffs become commits instead of DB blobs, which is the structured
  substrate DEV-391 wants for review feedback.

### Who does what

* **Orchestrator (only writer):** keeps a per-repo bare cache under
  `var/git-cache/<repo>.git` cloned from zooshly. To publish an attempt it
  makes a temp worktree at `base_sha`, applies the parsed
  `<<<FILE:>>>` output exactly where `spec_dir` receives it today, commits
  per role, pushes the attempt ref. One flock per repo cache serializes
  cross-spec git operations (the DEV-393 scheduler already serializes
  within a spec).
* **Mac runner (fetch-only):** `RunTestsRequest` gains `attempt_ref`;
  `workspace.worktree()` becomes `git fetch origin <attempt_ref>` +
  `worktree add --detach FETCH_HEAD`. The runner's `repos.yml` clones
  already have zooshly as origin. The runner holds **no push credential
  at all**.
* **Humans:** unaffected. `refs/auto/*` is outside the default fetch
  refspec, so a human `git pull` never downloads model-authored commits.
  Integration of a passed spec into a human branch stays a deliberate,
  human `git` action outside the pipeline.

## The four sharp edges — decisions

### 1. Isolation (security-relevant, cf. DEV-397/398)

* Dedicated SSH principal on zooshly (e.g. `coding-auto`), forced-command
  `git-shell`, used only by the orchestrator.
* A `pre-receive` hook on every bare repo enforces, for that principal:
  ref name matches `^refs/auto/spec/[A-Za-z0-9_-]+/attempt-[0-9]+$`;
  create-only (no updates, no deletes, no non-fast-forward); everything
  else — `refs/heads/*`, `HEAD`, tags — rejected outright.
* The runner authenticates as a separate read-only principal (or reuses
  its existing local clones' fetch path). Neither pipeline principal can
  write a ref a human might pull by accident.

### 2. Repo coverage (settle first — the ticket's own gate)

* zooshly stays the **single trust boundary**. Attempts never touch
  GitHub.
* ElectricSheep (GitHub-backed, not among the eleven zooshly bare repos)
  gets a zooshly mirror. Recommended mechanism: the Mac Studio working
  copy adds zooshly as a second push remote (`git remote set-url --add
  --push`), so the mirror updates on every human push with **no GitHub
  credential stored on zooshly** and no scheduled job to rot. The pipeline
  pins `base_sha` from the mirror; if the mirror lags GitHub, attempts
  build against a slightly older base — safe, visible, and fixed by the
  next human push.
* Pushing model-authored commits to GitHub is rejected as a materially
  different trust decision; if it is ever wanted, it is its own ticket.

### 3. Merge conflicts as a new failure class

* Policy: **attempts are never merged — not with each other, not with
  human branches — by the pipeline.** Every attempt (including the
  synthesis output, which becomes just `attempt-<n+1>`) is an independent
  branch from the same `base_sha`. Merge conflicts are impossible by
  construction; a retry "overwrites" exactly as today, except the prior
  attempt remains addressable instead of being a `retry_history/` copy.
* `git cherry-pick`/`merge-tree`-assisted synthesis is a possible later
  enhancement, explicitly out of scope here.

### 4. Secrets and durability (cf. DEV-199)

* No new data class crosses any boundary: a commit contains exactly the
  bytes that ship today as `patch_files` JSON. The existing sandbox and
  `.env` `InaccessiblePaths` discipline remain the primary control.
* Durability is bounded, not permanent: when a spec reaches
  DONE/FAILED/CANCELLED plus a retention window (proposed: 30 days), the
  orchestrator deletes its `refs/auto/spec/<id>/*` (via an admin-side
  sweep, since the pipeline principal itself cannot delete) and server-side
  `git gc` prunes the objects.
* Belt-and-braces: a local pre-push scan of the attempt diff for key-like
  patterns (reusing the exfil-gate patterns already in `test_runner`),
  refusing the push on a hit.

## What this deletes or simplifies

| Today | After |
|---|---|
| `_collect_patch_files` full-tree JSON dump, UTF-8/binary limits, payload size | gone — transport is a ref name |
| `_write_patch_files` + its path-escape jail | gone — `git worktree add` at the attempt commit |
| `mac_runner/integration.py` (DEV-399 heuristic) | `git diff --stat base_sha..attempt` empty ⇒ hard fail; guessed-layout check becomes a diff-path heuristic |
| `retry_history/` snapshots + `_read_retry_attempts` walker | attempt refs are the history |
| Synthesis prompt = full bodies × all attempts (~195k tokens observed) | per-attempt `git diff` + test summary — a fraction of the size, and DEV-393's remaining starvation window shrinks with it |
| Deletions inexpressible in a patch | ordinary `git rm` in the attempt commit |

## What stays

* Repo-less specs (pytest/node scratch projects with no `repo` in
  `test_strategy`) keep the current `spec_dir` flow untouched. Attempt
  branches apply only when a target repo exists.
* The `<<<FILE:>>>` parse layer, gates, supervisor, and scheduler are
  unchanged — this is transport and storage, not agent behavior.

## Failure modes

* **zooshly unreachable:** attempt publish or runner fetch fails with a
  clear transport error — same failure class and same operator story as
  "mac-runner unreachable" today. The spec stays re-runnable; nothing is
  half-applied (a ref either exists or does not).
* **Push rejected by hook:** treated as a pipeline bug, fails the task
  loudly. The hook is a tripwire, not a control flow.
* **Cache corruption:** `var/git-cache` is disposable; re-clone from
  zooshly.

## Rollout (smallest useful experiment first)

1. **Phase 0 — server prep:** `coding-auto` principal + pre-receive hook on
   the pilot repo only. Pilot: `JSONParser.git` (small, already on
   zooshly; `MetalGameOfLife.git` is the alternate).
2. **Phase 1 — dual-write:** orchestrator publishes attempt refs alongside
   the existing `patch_files` payload; runner still consumes payloads.
   Verify refs, trailers, and `git diff` sanity on a real spec run.
3. **Phase 2 — runner fetch behind a flag:** `AUTONOMOUS_GIT_TRANSPORT=1`
   switches the pilot repo's runs to `attempt_ref`; `patch_files` remains
   the fallback. Acceptance: one spec end-to-end (implementer commit →
   runner fetch → tests) on the pilot repo — the ticket's named
   experiment.
4. **Phase 3 — synthesis from diffs**, plus retention sweep.
5. **Phase 4 — deletions:** remove the payload path, `integration.py`,
   `retry_history`; extend the hook + mirror to the remaining repos;
   ElectricSheep mirror lands here at the latest.

Each phase is its own DEV ticket at implementation time (ticket-per-concern).

## Open questions for review

1. Pilot repo: `JSONParser.git` or `MetalGameOfLife.git`?
2. Retention window for closed specs' refs: 30 days?
3. ElectricSheep mirror via Mac Studio dual-push remote (recommended) or a
   zooshly-side scheduled fetch from GitHub (needs egress + a job to tend)?
4. Should reviewer feedback move to commit-anchored notes in the same
   change, or stay free-text until DEV-391 is scheduled? (This design only
   creates the substrate.)
