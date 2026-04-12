# Phase 2 Manual Test Plan

Human-in-the-loop verification of the autonomous execution pipeline.
Run these steps from the server machine. Each step includes what to
check and what to do if something goes wrong.

## Prerequisites

- [ ] qwen-server is running (`systemctl status qwen-server`)
- [ ] The `.env` file has `ADMIN_API_KEY` set
- [ ] The `.env` file has `JIRA_*` vars set (optional — if not set, Jira sync runs against the fake and gates are CLI-only)
- [ ] `pyyaml` is installed in the venv (`pip install pyyaml` — needed by the daemon to parse plan YAML)
- [ ] No model is currently loaded (or the server is idle) — the test will trigger multiple model swaps

## Test 1: Full happy path (spec → DONE)

This tests the entire pipeline end-to-end with real LLM inference.
Expect it to take 15-30 minutes depending on model load times.

### 1.1 Create a test spec

```bash
cat > /tmp/test_spec.md << 'EOF'
# Word frequency counter

## Goal
Build a Python 3.11 CLI that reads a text file and prints the top N
most frequent words, one per line, with their counts.

## Commands
- `wordfreq <file> [--top N]` — print the top N words (default 10)
- Words are case-insensitive, punctuation stripped
- Output format: `<count> <word>` sorted by count descending

## Acceptance criteria
- Reads from a file path given as the first argument
- `--top N` flag works (default 10)
- Case-insensitive (Hello == hello)
- Strips common punctuation (.,;:!?"'()-—)
- Exits with code 1 and a clear message if the file doesn't exist
- Handles empty files gracefully (prints nothing, exits 0)

## Constraints
- Python 3.11+ standard library only (no pip packages)
- Single file: `wordfreq.py`

## Test strategy
- Framework: pytest
- Required: yes
- Test file: `test_wordfreq.py`

## Output location
- New project (workspace-local)
EOF
```

### 1.2 Start the orchestrator daemon in the foreground

(Use a separate terminal so you can watch the logs.)

```bash
cd ~/Dev/qwen-server
source .env  # load ADMIN_API_KEY etc into shell
source venv/bin/activate
python orchestrator_daemon.py
```

### 1.3 Submit the spec

(In another terminal.)

```bash
cd ~/Dev/qwen-server
source venv/bin/activate
export ADMIN_API_KEY=<your key>
python qwen-autonomous submit /tmp/test_spec.md
```

**Check:** You should see a spec ID printed. Note it.

### 1.4 Watch the planner

The daemon will call q35_architect (Qwen3.5-122B) as the planner.
Watch the daemon logs. Within 2-5 minutes you should see either:

- `planner: needs clarification` — the planner wants more info.
  Run `python qwen-autonomous gates` to see the questions.
  Answer with: `python qwen-autonomous review <gate_id> --approve --notes "your answers"`
  The planner will re-run.

- `planner: produced YAML` — the spec was clear enough.
  Run `python qwen-autonomous gates` to see the plan_approval gate.

**Check:** The plan YAML should have `phases: [design, implement, test]`
and sensible acceptance criteria.

### 1.5 Approve the plan

```bash
python qwen-autonomous review <gate_id> --approve --notes "proceed"
```

### 1.6 Watch the architect

The daemon transitions the spec to EXECUTING and bootstraps 3 tasks.
The architect (q35_architect) runs next. This takes 5-15 minutes.

**Check in the daemon logs:**
- `bootstrapped 3 tasks from plan YAML`
- `calling agent=q35_architect, role=architect`
- `architect done, design_approval gate created`

**Check on disk:**
```bash
ls qwen_tasks_db/specs/<spec_id>/
# Should contain: spec.md, plan.yaml, design.md
```

### 1.7 Review and approve the design

```bash
python qwen-autonomous gates
# Look at the design_approval gate — it contains the full design.md
python qwen-autonomous review <gate_id> --approve
```

If the design is bad: `--reject --notes "fix: <specific issue>"`
The architect will re-run with your notes.

### 1.8 Watch the implementer

The implementer (Qwen3.5-35B, default) runs. Takes 3-8 minutes.

**Check in the daemon logs:**
- `calling agent=implementer, role=implementer`
- `implementer done (N files, retry=0), code_review gate created`

**Check on disk:**
```bash
ls qwen_tasks_db/specs/<spec_id>/
# Should contain: spec.md, plan.yaml, design.md, wordfreq.py
cat qwen_tasks_db/specs/<spec_id>/wordfreq.py
# Should be a working Python script
```

### 1.9 Review and approve the code

```bash
python qwen-autonomous gates
python qwen-autonomous review <gate_id> --approve
```

If the code is bad: `--reject --notes "issue: ..."`. The implementer
will re-run (up to 3 retries).

### 1.10 Watch the reviewer

The reviewer (Coder-30B HD) writes tests and runs them via pytest.

**Check in the daemon logs:**
- `calling agent=reviewer, role=reviewer`
- `running tests: python3 -m pytest ...`
- Either `test result: PASS` or `test result: FAIL`

If tests PASS: a release_approval gate is created.
If tests FAIL: the implementer retries automatically. Watch for
`tests failed, retrying implementer (attempt N/3)`.

### 1.11 Final approval

```bash
python qwen-autonomous gates
# Should show a release_approval gate with test results
python qwen-autonomous review <gate_id> --approve
```

### 1.12 Verify completion

```bash
python qwen-autonomous status <spec_id>
# Should show: status: done
```

**Check the workspace has all artifacts:**
```bash
ls -la qwen_tasks_db/specs/<spec_id>/
# Expected: spec.md, plan.yaml, design.md, wordfreq.py,
#           test_wordfreq.py (or similar), review_report.md,
#           test_output.txt
```

**Try running the generated code:**
```bash
cd qwen_tasks_db/specs/<spec_id>/
echo "hello world hello foo bar hello" > /tmp/test_input.txt
python wordfreq.py /tmp/test_input.txt --top 3
# Should print: 3 hello, then 1 world (or similar)
```

### 1.13 Check Jira (if configured)

Open your AUTO project board. You should see:
- One epic: "Word frequency counter"
- Stories for each gate (design_approval, code_review, release_approval)
- Comments on the epic from agent runs and test runs
- All stories in Done/Approved status

---

## Test 2: Rejection and retry path

### 2.1 Submit the same spec again

```bash
python qwen-autonomous submit /tmp/test_spec.md
```

### 2.2 Approve the plan normally

### 2.3 When the architect produces a design, reject it

```bash
python qwen-autonomous review <gate_id> --reject \
  --notes "The design is missing error handling for malformed files. Add a section on input validation."
```

**Check:** The architect re-runs. The new design should address your
feedback. If it does, approve. If not, reject again (up to you).

### 2.4 When the implementer produces code, reject it

```bash
python qwen-autonomous review <gate_id> --reject \
  --notes "The --top flag parsing is broken. Use argparse instead of manual sys.argv handling."
```

**Check:** The implementer re-runs. The retry_count increments.
The daemon log shows `retrying implementer (attempt N/3)`.

### 2.5 Verify the retry counter

```bash
python qwen-autonomous status <spec_id>
# The events log should show multiple agent_ran events for implementer
python qwen-autonomous events <spec_id>
```

---

## Test 3: Automatic test failure retry

### 3.1 Submit a spec that's likely to have bugs on first attempt

```bash
cat > /tmp/hard_spec.md << 'EOF'
# Roman numeral converter

## Goal
Python 3.11 CLI: `roman <number>` prints the Roman numeral, `roman --from <numeral>` prints the integer.

## Acceptance criteria
- Handles 1-3999
- Validates input (non-integer, out of range, invalid numeral)
- Exits with code 1 and clear error message on invalid input
- Subtractive notation (IV not IIII, IX not VIIII, etc.)

## Constraints
- Python 3.11+ stdlib only
- Single file: roman.py

## Test strategy
- Framework: pytest
- Required: yes
EOF

python qwen-autonomous submit /tmp/hard_spec.md
```

### 3.2 Approve all gates normally

Let the pipeline run. The reviewer's tests are likely to catch edge
cases the implementer missed.

**What to watch for:**
- `tests failed, retrying implementer (attempt 1/3)` — this is the
  retry loop in action
- The implementer gets the test failure output as feedback
- On retry, the implementer should produce improved code

### 3.3 If it exhausts retries

After 3 failed attempts, the spec transitions to FAILED.

```bash
python qwen-autonomous status <spec_id>
# status: failed
```

Check the `failure_report.md` and `test_output.txt` in the workspace
to understand what went wrong.

---

## Test 4: Jira reverse sync (requires live Jira)

### 4.1 Submit a spec and let it reach a review gate

### 4.2 Instead of using the CLI, approve the gate in the Jira UI

- Go to the AUTO board
- Find the story for the gate
- Move it to "Done" (drag it on the board, or use the status dropdown)
- Optionally add a comment with your notes

### 4.3 Watch the daemon logs

Within 10 seconds (the Jira sync poll interval), you should see:
```
jira-sync: reverse-synced gate <gate_id> as approved (from issue AUTO-N)
```

The daemon should then advance the spec to the next phase, just as if
you'd used the CLI.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Daemon hangs for >45 minutes | Agent inference timed out | Check qwen-server logs; the model may have OOM'd or stalled. Restart qwen-server. |
| `No <<<DESIGN>>>` parse error | Agent didn't follow the output format | Check the spec clarity. Try a simpler spec. Check the raw response in the events log. |
| `No <<<FILE:` parse error | Same — implementer didn't produce file blocks | Same remediation. The implementer system prompt is very explicit; this usually means the model was confused by the spec. |
| Tests fail repeatedly | Generated code has real bugs | Check `test_output.txt` in the workspace. The test failures are real — the model wrote buggy code. After 3 retries it gives up. You can manually fix the code and re-submit. |
| Jira sync doesn't fire | JIRA_URL/EMAIL/TOKEN not set or invalid | Check daemon startup logs for "Jira sync running with FakeJiraClient". Re-run the smoke test from Phase 1c. |
| `yaml.safe_load` fails | Plan YAML is malformed | Check `plan.yaml` in the workspace. The planner occasionally produces invalid YAML. Reject the plan gate and let it retry. |

---

## What success looks like

After Test 1, you should have:
- A spec that went from markdown → YAML plan → architecture → code → tests → DONE
- Each transition gated by your explicit approval
- All artifacts in the workspace directory
- Generated code that actually runs
- If Jira is configured: a complete board with epic + stories reflecting the full lifecycle

The system is now capable of taking a specification and autonomously
developing, testing, and presenting software for your review. The human
stays in the loop at every major transition. The agents do the work;
you decide when it's good enough.
