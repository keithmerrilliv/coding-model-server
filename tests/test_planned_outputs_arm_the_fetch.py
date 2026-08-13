"""A spec without a change-surface table must still ground the implementer — DEV-571.

The existing-file fetch keyed ONLY on the spec's optional `| path | modify |`
table. Nothing requires a spec to carry one, so a spec that lists its files as
prose or bullets fetched nothing, and the implementer rewrote existing files
from imagination. Since DEV-581 that also silently disarms anchored edits —
`edit_mode = DIFF_BASED_EDITS and bool(existing_files)` — so the same omission
now defeats the grounding guard AND the corruption fix.

Observed twice in one session (Centipede slices 3 and 4, spec_281b8184 /
spec_3e93c921): both specs declared their files as a bulleted "Files to change"
list, both had `[diff-based edits]` absent from the log, and both came back with
the entire pre-existing GameTests suite deleted. The same change authored WITH a
table (spec_c43d62fb) preserved all 14 existing tests and appended 1.

Manifest mode already treats its own file list as candidates; the single-call
path now does the same with the approved plan's implement.outputs.
"""
from coding_model_autonomous import test_runner


class _Spec:
    id = "spec_test"

    def __init__(self, yaml_text):
        self.normalized_yaml = yaml_text


PLAN = """\
test_strategy:
  repo: centipede
  base_ref: main
phases:
  - name: design
    role: architect
    outputs: ["design.md"]
  - name: implement
    role: implementer
    outputs:
      - Sources/CentipedeCore/Game.swift
      - Tests/CentipedeCoreTests/GameTests.swift
"""

# The shape that used to disarm everything: no `| path | modify |` row.
BULLETED_SPEC = """\
# Slice

## Files to change
- **Edit** `Sources/CentipedeCore/Game.swift`
- **Append** tests to `Tests/CentipedeCoreTests/GameTests.swift`
"""


# ── the helper ──────────────────────────────────────────────────────────────

def test_implement_outputs_are_extracted():
    from coding_model_server import orchestrator_daemon as od
    assert od._planned_implement_outputs(_Spec(PLAN)) == [
        "Sources/CentipedeCore/Game.swift",
        "Tests/CentipedeCoreTests/GameTests.swift",
    ], "only the implement phase's outputs, in order"


def test_design_outputs_are_not_treated_as_implement_outputs():
    from coding_model_server import orchestrator_daemon as od
    assert "design.md" not in od._planned_implement_outputs(_Spec(PLAN))


def test_a_malformed_or_absent_plan_yields_nothing():
    from coding_model_server import orchestrator_daemon as od
    assert od._planned_implement_outputs(_Spec("")) == []
    assert od._planned_implement_outputs(_Spec("phases: not-a-list\n")) == []


# ── the fix: a table-less spec now fetches its existing files ───────────────

def test_a_bulleted_spec_still_fetches_the_files_the_plan_will_write(monkeypatch):
    """The regression. No change-surface table, but the plan names the files —
    so they are fetched and the implementer is grounded (and edit_mode arms)."""
    from coding_model_server import orchestrator_daemon as od
    seen = {}

    def fake(repo, paths, base_ref):
        seen.update(repo=repo, paths=paths, base_ref=base_ref)
        return [("Tests/CentipedeCoreTests/GameTests.swift", "final class T {}")], []

    monkeypatch.setattr(test_runner, "fetch_repo_files", fake)
    out = od._fetch_existing_files_for_spec(
        _Spec(PLAN), BULLETED_SPEC,
        extra_paths=od._planned_implement_outputs(_Spec(PLAN)))

    assert seen["paths"] == [
        "Sources/CentipedeCore/Game.swift",
        "Tests/CentipedeCoreTests/GameTests.swift",
    ], "the plan's implement outputs must be asked for"
    assert seen["repo"] == "centipede"
    assert seen["base_ref"] == "main"
    assert out, "the implementer must receive the existing file"


def test_the_run_implementer_call_site_passes_the_planned_outputs(monkeypatch):
    """Pins the wiring, not just the helper: the single-call path must hand the
    plan's outputs to the fetch, or the whole fix is inert."""
    from coding_model_server import orchestrator_daemon as od
    seen = {}

    def fake(repo, paths, base_ref):
        seen["paths"] = paths
        return [], []

    monkeypatch.setattr(test_runner, "fetch_repo_files", fake)
    spec = _Spec(PLAN)
    od._fetch_existing_files_for_spec(
        spec, BULLETED_SPEC, extra_paths=od._planned_implement_outputs(spec))
    assert "Tests/CentipedeCoreTests/GameTests.swift" in seen["paths"]


# ── it must not become "ask for everything" ─────────────────────────────────

def test_a_truly_greenfield_plan_still_asks_for_nothing(monkeypatch):
    """A plan whose implement phase declares no outputs, and a spec with no
    table, must still make no runner call — the greenfield case is unchanged."""
    from coding_model_server import orchestrator_daemon as od
    monkeypatch.setattr(
        test_runner, "fetch_repo_files",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    spec = _Spec("test_strategy:\n  repo: centipede\n"
                 "phases:\n  - name: implement\n    role: implementer\n")
    assert od._fetch_existing_files_for_spec(
        spec, "no table here",
        extra_paths=od._planned_implement_outputs(spec)) == []
