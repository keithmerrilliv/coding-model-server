"""DEV-633: one place sums every prompt section against the destination window.

Each section used to clamp itself at its own char knob and nothing added them
up, so the worst case was N knobs' worth of prompt and an operator tuning one
knob could not see the others. Run 20's ``AUTONOMOUS_EXISTING_FILES_MAX_CHARS``
override raised the protected ceiling 7.5x because the two shared a constant
(DEV-627); run 21's implementer prompt went past 1 MB into a 413. The fit
check meant to catch that (DEV-624) ran after the prompt was rendered and
could only move it to a bigger agent, never make it smaller.
"""
from unittest import mock

import pytest

import coding_model_autonomous.executor as ex
import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import context as ctx


def _files(**sizes) -> list[tuple[str, str]]:
    return [(path, "x" * n) for path, n in sizes.items()]


def _section(name, *, chars, knob=None, files=1):
    each = chars // files
    return ctx.Section(name, [(f"{name}{i}.py", "x" * each) for i in range(files)],
                       knob)


# ── the sum ──────────────────────────────────────────────────────────────────

class TestAggregateSum:
    def test_sections_share_one_window_instead_of_stacking_knobs(self):
        """The regression itself. Two sections at 60K each is 120K of prompt;
        a window with room for ~90K of chars must not hand out both knobs."""
        window = 40_000                           # tokens
        alloc = ctx.allocate(
            [_section("editable", chars=60_000, knob=60_000, files=6),
             _section("protected", chars=60_000, knob=60_000, files=6)],
            fixed_chars=0, window_tokens=window, completion_tokens=8_000)
        assert alloc.prompt_chars <= alloc.input_budget_chars
        assert alloc.needed_tokens <= window
        # Both knobs together (120K chars = 40K tokens) plus the completion
        # would not have fit; the second section is the one that gives.
        assert alloc.sections["editable"].chars == 60_000
        assert alloc.dropped("protected")

    def test_a_sum_that_fits_gives_every_section_its_knob(self):
        """The knob is a preference the allocator honours when there is room —
        with a big window nothing changes from the pre-DEV-633 render."""
        alloc = ctx.allocate(
            [_section("editable", chars=50_000, knob=60_000, files=5),
             _section("protected", chars=50_000, knob=60_000, files=5)],
            fixed_chars=1_000, window_tokens=262_144, completion_tokens=16_000)
        assert not alloc.dropped_any
        assert alloc.sections["editable"].chars == 50_000
        assert alloc.sections["protected"].chars == 50_000

    def test_a_knob_is_still_a_ceiling(self):
        """Room in the window does not let a section spend past its knob."""
        alloc = ctx.allocate([_section("editable", chars=90_000, knob=30_000, files=9)],
                             fixed_chars=0, window_tokens=262_144,
                             completion_tokens=8_000)
        assert alloc.sections["editable"].chars <= 30_000
        assert alloc.dropped("editable")

    def test_priority_order_decides_who_gives(self):
        """Sections are allocated in the order they are passed: the declared
        modification set first, protected next, prior artifacts last."""
        first, second = _section("a", chars=90_000, knob=90_000, files=9), \
            _section("b", chars=90_000, knob=90_000, files=9)
        alloc = ctx.allocate([first, second], fixed_chars=0,
                             window_tokens=40_000, completion_tokens=8_000)
        assert not alloc.dropped("a")
        assert alloc.dropped("b")

    def test_the_fixed_part_is_charged_before_any_section(self):
        """Spec, design and instructions are not droppable, so they come out
        of the window first — a huge design leaves the sections less."""
        small = ctx.allocate([_section("editable", chars=60_000, knob=60_000, files=6)],
                             fixed_chars=1_000, window_tokens=30_000,
                             completion_tokens=8_000)
        large = ctx.allocate([_section("editable", chars=60_000, knob=60_000, files=6)],
                             fixed_chars=60_000, window_tokens=30_000,
                             completion_tokens=8_000)
        assert large.sections["editable"].chars < small.sections["editable"].chars

    def test_the_completion_budget_is_reserved_not_discovered(self):
        """DEV-543 watched the architect spend its whole output budget; the
        input may not claim the tokens the answer needs."""
        lean = ctx.allocate([_section("editable", chars=200_000, knob=200_000, files=20)],
                            fixed_chars=0, window_tokens=65_536, completion_tokens=1_000)
        fat = ctx.allocate([_section("editable", chars=200_000, knob=200_000, files=20)],
                           fixed_chars=0, window_tokens=65_536, completion_tokens=48_000)
        assert fat.sections["editable"].chars < lean.sections["editable"].chars
        assert fat.needed_tokens <= 65_536

    def test_an_unknown_window_applies_no_pressure(self):
        """An agent that is not in the config keeps the pre-DEV-633 behaviour:
        every section gets its own knob and nothing is summed."""
        alloc = ctx.allocate(
            [_section("editable", chars=60_000, knob=60_000, files=6),
             _section("protected", chars=60_000, knob=60_000, files=6)],
            fixed_chars=0, window_tokens=None, completion_tokens=8_000)
        assert not alloc.dropped_any
        assert alloc.fits

    def test_a_dropped_file_is_named_never_silently_gone(self):
        alloc = ctx.allocate([_section("editable", chars=90_000, knob=10_000, files=9)],
                             fixed_chars=0, window_tokens=262_144,
                             completion_tokens=8_000)
        assert alloc.dropped("editable")
        assert all(p.startswith("editable") for p in alloc.dropped("editable"))

    def test_a_later_small_file_still_fits_after_a_big_one_is_skipped(self):
        """The greedy fill skips rather than stops, so one oversized file at
        the head of the list does not cost every file behind it."""
        section = ctx.Section("editable", _files(**{"huge.py": 50_000, "tiny.py": 10}),
                              None)
        alloc = ctx.allocate([section], fixed_chars=0, window_tokens=1_000,
                             completion_tokens=0)
        assert alloc.dropped("editable") == ["huge.py"]
        assert [p for p, _ in alloc.files("editable")] == ["tiny.py"]


# ── choosing the agent and the sections together ─────────────────────────────

class TestPlanDispatch:
    windows = {"small": 10_000, "big": 200_000, None: None}

    def _plan(self, sections, *, agent="small", fixed=0, completion=1_000,
              candidates=("small", "big"), reserve=None):
        return ctx.plan_dispatch(
            "spec_t", role="implementer", sections=sections, fixed_chars=fixed,
            completion_tokens=completion, agent=agent, candidates=candidates,
            window_of=self.windows.get, reserve_of=reserve)

    def test_a_prompt_that_fits_stays_on_the_chosen_agent(self):
        """The architect's complexity recommendation picked the agent for a
        reason; a prompt that fits it is not moved."""
        alloc = self._plan([_section("editable", chars=1_000, knob=60_000)])
        assert alloc.agent == "small"
        assert not alloc.dropped_any

    def test_escalation_beats_shedding(self):
        """A bigger window is always better than less context: nothing is
        dropped while any candidate can hold the whole prompt (DEV-624)."""
        alloc = self._plan([_section("editable", chars=120_000, knob=200_000, files=12)])
        assert alloc.agent == "big"
        assert not alloc.dropped_any

    def test_shedding_only_after_every_window_is_tried(self):
        alloc = self._plan([_section("editable", chars=900_000, knob=900_000, files=90)])
        assert alloc.agent == "big"            # the largest available
        assert alloc.dropped("editable")
        assert alloc.fits

    def test_the_sum_is_journalled_on_every_dispatch(self, caplog):
        """Run 26 dispatched five implementer prompts and the journal said
        nothing about any of them, because the allocator only spoke when it
        had to escalate or cut. A guard that is silent when it passes cannot
        be shown to have been armed."""
        import logging
        with caplog.at_level(logging.INFO, logger="orchestrator.context"):
            self._plan([_section("editable", chars=1_000, knob=60_000)])
        budget_lines = [r.getMessage() for r in caplog.records
                        if "prompt budget" in r.getMessage()]
        assert len(budget_lines) == 1
        # The line has to carry the numbers an operator would act on.
        assert "editable" in budget_lines[0]
        assert "completion" in budget_lines[0]

    def test_an_unknown_candidate_never_wins_an_oversized_prompt(self):
        """An agent missing from the config estimates as unbounded, so it
        would win every oversized prompt for the worst possible reason."""
        alloc = self._plan([_section("editable", chars=900_000, knob=900_000, files=90)],
                           agent="small", candidates=("mystery", "big"))
        assert alloc.agent == "big"

    def test_an_unknown_pick_is_still_left_alone(self):
        """The chosen agent is exempt: an unknown pick behaves as it did
        before any of this existed."""
        alloc = self._plan([_section("editable", chars=900_000, knob=900_000, files=90)],
                           agent="mystery", candidates=())
        assert alloc.agent == "mystery"
        assert not alloc.dropped_any

    def test_no_window_at_all_refuses_before_the_call(self):
        """The fixed part alone overflows every window. Nothing is droppable,
        so this is refused up front instead of becoming a 413 the model
        server discovers after the dispatch."""
        with pytest.raises(ctx.PromptTooLarge):
            self._plan([], fixed=3_000_000)

    def test_the_reasoning_budget_is_reserved_like_the_completion(self):
        """DEV-616's --reasoning-budget is spent in the same window and before
        the completion, so it comes off the input's share too."""
        # "small" holds 10_000 tokens: 9_500 after headroom, less a 1_000
        # completion = 8_500 tokens = 25_500 chars of input. A 4_000-token
        # reasoning budget takes 12_000 of those chars away.
        sections = [_section("editable", chars=60_000, knob=60_000, files=60)]
        without = self._plan(sections, agent="small", candidates=())
        with_think = self._plan(sections, agent="small", candidates=(),
                                reserve=lambda a: 4_000)
        assert without.sections["editable"].chars == 25_000   # 25 × 1_000
        assert with_think.sections["editable"].chars == 13_000  # 13 × 1_000


# ── the wiring: knobs, windows, reserves ─────────────────────────────────────

class TestDaemonWiring:
    def test_windows_come_from_the_agent_config(self):
        assert d._agent_ctx_limit("deep_implementer") == 262_144
        assert d._agent_ctx_limit("no_such_agent") is None

    def test_the_reasoning_reserve_is_read_from_the_server_args(self):
        """qwen38_architect ships --reasoning-budget 4096 (DEV-616)."""
        assert d._agent_reasoning_reserve("qwen38_architect") == 4096

    def test_an_agent_without_a_reasoning_budget_reserves_nothing(self):
        assert d._agent_reasoning_reserve("deep_implementer") == 0
        assert d._agent_reasoning_reserve(None) == 0

    def test_raising_one_knob_no_longer_raises_the_other_section(self):
        """DEV-627's regression, now structural rather than a second constant:
        an operator raising the editable knob cannot buy the protected section
        room the window does not have."""
        sections = [
            ctx.Section(ctx.SECTION_EDITABLE, _files(**{"a.py": 400_000}),
                        400_000),
            ctx.Section(ctx.SECTION_PROTECTED, _files(**{"p.py": 400_000}),
                        400_000),
        ]
        alloc = d._prompt_budget("spec_t", "implementer", fixed_chars=0,
                                 completion_tokens=16_000, agent="implementer",
                                 sections=sections)
        assert alloc.needed_tokens <= d._agent_ctx_limit("implementer")


# ── the render still says what it did not show ───────────────────────────────

class TestOmissionsReachThePrompt:
    def test_an_emptied_existing_section_still_renders_its_omissions(self):
        """A section the allocator emptied must not simply vanish — "you were
        not shown X" is actionable, a missing section is not, and a model that
        thinks it saw everything emits an invention (DEV-492)."""
        text = "\n".join(m["content"] for m in ex.build_implementer_message(
            "spec", "design", existing_files=[],
            omitted_existing=["src/World.swift"]))
        assert "src/World.swift" in text
        assert "Not shown" in text and "Do not emit them" in text

    def test_dropped_protected_files_stay_off_limits_in_the_prompt(self):
        out = ex._render_reference_files([], omitted=["Scaffold/Field.swift"])
        assert "may NOT change" in out
        assert "Scaffold/Field.swift" in out
        assert "still off-limits" in out

    def test_the_architect_is_told_which_modified_files_it_did_not_read(self):
        """Its editable render was the one wholly unbudgeted file section, and
        the framing promised "every file you may modify is shown here in full"
        — a promise a trimmed section must not keep making."""
        text = "\n".join(m["content"] for m in ex.build_architect_message(
            "spec", existing_files=[("a.py", "A = 1")],
            omitted_existing=["src/big.py"]))
        assert "src/big.py" in text and "Not shown" in text
        assert "you have not read them" in text.lower()
        assert "every file you may modify is shown here in full" not in text

    def test_a_full_architect_section_keeps_its_original_promise(self):
        text = "\n".join(m["content"] for m in ex.build_architect_message(
            "spec", existing_files=[("a.py", "A = 1")]))
        assert "every file you may modify is shown here in full" in text
        assert "Not shown" not in text

    def test_the_reviewer_is_told_not_to_judge_what_it_never_saw(self):
        text = "\n".join(m["content"] for m in ex.build_reviewer_message(
            "spec", "design", [("a.py", "A = 1")],
            omitted_code=["src/unread.py"]))
        assert "src/unread.py" in text
        assert "Do not judge them" in text

    def test_an_unbudgeted_caller_still_clamps_at_the_knob(self):
        """The renderers keep their own ceiling for any caller that renders
        without budgeting first — the allocator never hands them more than the
        knob, so this only ever bites outside the budgeted paths."""
        with mock.patch.object(ex, "PROTECTED_FILES_MAX_CHARS", 100):
            out = ex._render_reference_files([("src/big.py", "z" * 10_000)])
        assert "z" * 10_000 not in out
        assert "src/big.py" in out
