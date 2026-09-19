"""DEV-730: the architect is told when a declared modification could not be read.

Run 42 served the architect zero editable files (DEV-601's path bug). The
prompt's editable section is guarded by `if existing_files or omitted_existing`,
so with nothing to show it rendered nothing at all — and the one shape where
silence is most expensive is the one with no section to speak in. Attempt 0
designed blind and was discarded. Attempt 1, after a rejection nudged it toward
inspecting, asked for exactly the right three files with the DEV-714 tool loop
and got them. The capability was unchanged between the two; the trigger was
failure rather than noticing the gap, and DEV-431 sends retries to weaker
models that may not notice at all.

Two properties matter and pull against each other, so both are pinned here:
the section must ARM on the run-42 shape (where every omission reason is git's
"does not exist", which `omission_status` calls ABSENT), and it must be
completely absent from the normal path, which is most dispatches.
"""
from coding_model_autonomous.context import (
    Omission, SECTION_EDITABLE, SpecContext)
from coding_model_autonomous.executor import build_architect_message

HEADING = "could not be read (DEV-730)"

# Verbatim from run 42's read_files answer, which is where the ambiguity
# DEV-601 exploits lives: a WRONG path and an absent file say this.
GIT_ABSENT = "fatal: path 'Audioscape.swift' does not exist in 'main'"


def _user(msgs):
    return msgs[1]["content"]


def _ctx(**kw):
    base = dict(spec_id="s1", repo="electric-sheep", base_ref="main",
                candidates=[], declared=[], protected_paths=[])
    base.update(kw)
    return SpecContext(**base)


# ── SpecContext.unreadable_modifications ────────────────────────────────────

class TestWhichFilesAreReported:
    def test_a_declared_file_that_was_served_is_not_reported(self):
        ctx = _ctx(declared=["src/x.py"], editable={"src/x.py": "code"})
        assert ctx.unreadable_modifications() == []

    def test_a_declared_file_that_was_omitted_is_reported_with_its_reason(self):
        ctx = _ctx(declared=["src/x.py"],
                   omitted=[Omission("src/x.py", SECTION_EDITABLE, "boom")])
        out = ctx.unreadable_modifications()
        assert [o.path for o in out] == ["src/x.py"]
        assert out[0].reason == "boom"

    def test_the_run_42_shape_arms(self):
        """Three declared modifications, none served, every reason ABSENT.

        This is the case a status filter would have silently dropped — and it
        is the only case the ticket was filed from.
        """
        declared = ["ElectricSheep/HallucinationForcer.swift",
                    "ElectricSheep/MetricsParticleBridge.swift",
                    "ElectricSheep/ElectricSheepApp.swift"]
        ctx = _ctx(declared=declared, editable={},
                   omitted=[Omission(p, SECTION_EDITABLE, GIT_ABSENT)
                            for p in declared])
        assert [o.path for o in ctx.unreadable_modifications()] == declared

    def test_a_declared_file_with_no_omission_row_still_reports(self):
        """Absence of an answer is not an answer (see the DEV-698 lesson):
        a path nobody recorded anything about is still one the architect
        has not seen."""
        out = _ctx(declared=["src/x.py"]).unreadable_modifications()
        assert len(out) == 1
        assert "not returned" in out[0].reason

    def test_a_declared_file_served_as_protected_is_not_reported(self):
        """It is in the prompt, read-only. Declaring it modifiable is a spec
        defect, but it is not an unread file and must not be reported as one
        — that would send the architect to read what it is already holding."""
        ctx = _ctx(declared=["src/p.py"], protected_paths=["src/p.py"],
                   protected={"src/p.py": "code"})
        assert ctx.unreadable_modifications() == []

    def test_no_repo_reports_nothing(self):
        """Greenfield: there is nothing to read from, every path is a creation
        by construction, and no role can be told otherwise."""
        ctx = _ctx(repo=None, declared=["src/x.py"])
        assert ctx.unreadable_modifications() == []


# ── the prompt section ──────────────────────────────────────────────────────

class TestTheNormalPathIsUntouched:
    """Acceptance criterion 3: no new noise on a complete editable set."""

    def test_no_section_without_unreadable_files(self):
        for kwargs in ({}, {"unreadable": None}, {"unreadable": []}):
            body = _user(build_architect_message(
                "# Spec", existing_files=[("src/x.py", "code")], **kwargs))
            assert HEADING not in body

    def test_the_prompt_is_byte_identical_to_before(self):
        served = [("src/x.py", "code")]
        assert (build_architect_message("# Spec", existing_files=served,
                                        base_ref="main", unreadable=[])
                == build_architect_message("# Spec", existing_files=served))


class TestTheSectionSaysWhatIsMissing:
    UNREADABLE = [("ElectricSheep/Audioscape.swift", "absent", GIT_ABSENT)]

    def test_it_renders_with_an_entirely_empty_editable_set(self):
        """The run-42 shape. The editable section does not render at all here,
        which is exactly why this one lives outside it."""
        body = _user(build_architect_message(
            "# Spec", existing_files=[], unreadable=self.UNREADABLE,
            base_ref="main", tools=True))
        assert "files the plan will MODIFY" not in body
        assert HEADING in body

    def test_it_counts_the_gap_and_names_every_path_and_reason(self):
        body = _user(build_architect_message(
            "# Spec", existing_files=[("src/shown.py", "code")],
            unreadable=[("a/One.swift", "absent", GIT_ABSENT),
                        ("b/Two.swift", "unknown", "read timed out")],
            base_ref="main"))
        assert "2 of the 3 file(s)" in body
        assert "`a/One.swift`" in body and "`b/Two.swift`" in body
        assert GIT_ABSENT in body and "read timed out" in body
        assert "`main`" in body

    def test_it_refuses_to_present_the_reason_as_a_verdict(self):
        """git says the same sentence for a wrong path and a missing file.
        DEV-604 turned that ambiguity into three regenerated files."""
        body = _user(build_architect_message(
            "# Spec", unreadable=self.UNREADABLE, base_ref="main"))
        assert "not what it means" in body


class TestTheAffordanceTracksTheToolOffer:
    UNREADABLE = [("a/One.swift", "absent", GIT_ABSENT)]

    def test_with_tools_it_names_the_tool_and_invites_a_corrected_path(self):
        body = _user(build_architect_message(
            "# Spec", unreadable=self.UNREADABLE, base_ref="main", tools=True))
        assert "<<<READ_FILE>>>" in body
        assert "same basename under a directory" in body

    def test_without_tools_it_offers_no_tool_it_cannot_honour(self):
        """`tools` is False when ARCHITECT_TOOLS is off, when the spec names
        no repo, and when the budget withdrew the offer. Advertising a tool
        the dispatch will not answer is worse than saying nothing."""
        body = _user(build_architect_message(
            "# Spec", unreadable=self.UNREADABLE, base_ref="main", tools=False))
        assert HEADING in body
        assert "READ_FILE" not in body
        assert "do NOT assume these files are new" in body


def test_the_gap_is_stated_before_the_specification():
    """It has to change how the spec is read, so it cannot come after it."""
    body = _user(build_architect_message(
        "# SPEC-MARKER", unreadable=[("a/One.swift", "absent", GIT_ABSENT)],
        base_ref="main", tools=True))
    assert body.index(HEADING) < body.index("# SPEC-MARKER")
