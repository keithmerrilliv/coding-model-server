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

from coding_model_autonomous import settings
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

    def test_a_knob_is_a_preference_not_a_ceiling(self):
        """DEV-648: a knob is what a section ASKS for. Room the window still
        has after every section has had its preference goes back to whoever
        was cut — run 28's architect was shown ZERO editable files because the
        one file the plan modifies is 146K and the knob is 60K, against a
        window with 343K chars free."""
        alloc = ctx.allocate([_section("editable", chars=90_000, knob=30_000, files=9)],
                             fixed_chars=0, window_tokens=262_144,
                             completion_tokens=8_000)
        assert alloc.sections["editable"].chars == 90_000
        assert not alloc.dropped("editable")

    def test_one_file_bigger_than_its_whole_knob_is_still_shown(self):
        """The run-28 shape exactly: a single editable file larger than the
        section's entire knob, inside a window that holds it easily."""
        big = [("src/coding_model_autonomous/executor.py", "x" * 146_000)]
        alloc = ctx.allocate([ctx.Section(ctx.SECTION_EDITABLE, big, 60_000)],
                             fixed_chars=17_517, window_tokens=131_072,
                             completion_tokens=10_000)
        assert not alloc.dropped(ctx.SECTION_EDITABLE)
        assert alloc.files(ctx.SECTION_EDITABLE) == big
        assert alloc.fits

    def test_the_refill_still_respects_priority(self):
        """Leftover room goes back in priority order, so the declared
        modification set is made whole before the protected tree."""
        alloc = ctx.allocate(
            [_section("editable", chars=80_000, knob=20_000, files=8),
             _section("protected", chars=80_000, knob=20_000, files=8)],
            fixed_chars=0, window_tokens=40_000, completion_tokens=0)
        # 40_000 * 0.95 * 3 = 114_000 chars for 160_000 of content.
        assert not alloc.dropped("editable")          # made whole first
        assert alloc.dropped("protected")             # pays for it
        assert alloc.fits

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
        """A window that genuinely cannot hold the section — after DEV-648's
        refill, that is the only reason a file is still missing."""
        alloc = ctx.allocate([_section("editable", chars=900_000, knob=900_000, files=90)],
                             fixed_chars=0, window_tokens=20_000,
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
        alloc = self._plan([_section("protected", chars=900_000, knob=900_000, files=90)])
        assert alloc.agent == "big"            # the largest available
        assert alloc.dropped("protected")
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
        alloc = self._plan([_section("protected", chars=900_000, knob=900_000, files=90)],
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
        sections = [_section("protected", chars=60_000, knob=60_000, files=60)]
        without = self._plan(sections, agent="small", candidates=())
        with_think = self._plan(sections, agent="small", candidates=(),
                                reserve=lambda a: 4_000)
        assert without.sections["protected"].chars == 25_000   # 25 × 1_000
        assert with_think.sections["protected"].chars == 13_000  # 13 × 1_000


# ── DEV-927: the modification set outranks references, or the prompt is refused ──

class TestEditableOutranksProtected:
    windows = {"small": 10_000, "big": 200_000, None: None}

    def _plan(self, sections, *, agent="big", candidates=(), fixed=0,
              completion=1_000, role="architect"):
        return ctx.plan_dispatch(
            "spec_t", role=role, sections=sections, fixed_chars=fixed,
            completion_tokens=completion, agent=agent, candidates=candidates,
            window_of=self.windows.get)

    def test_protected_yields_before_an_editable_file_is_dropped(self):
        """Run 37's architect: a modest knob left the big editable file dropped
        in pass one, pass two could only refill what protected had not taken,
        and 243K chars of references were kept over the one file the design
        had to change. "big" holds 190_000 tokens = 570_000 chars."""
        sections = [
            ctx.Section(ctx.SECTION_EDITABLE, _files(**{"daemon.py": 400_000}), 60_000),
            ctx.Section(ctx.SECTION_PROTECTED,
                        _files(**{"a.py": 150_000, "b.py": 150_000, "c.py": 100_000}),
                        600_000),
        ]
        alloc = self._plan(sections)
        assert alloc.files(ctx.SECTION_EDITABLE) == sections[0].files
        assert not alloc.dropped(ctx.SECTION_EDITABLE)
        # What is left after the editable file goes to references, by size.
        assert [p for p, _ in alloc.files(ctx.SECTION_PROTECTED)] == ["a.py"]
        assert set(alloc.dropped(ctx.SECTION_PROTECTED)) == {"b.py", "c.py"}
        assert alloc.fits

    def test_a_prompt_that_already_fits_is_not_rearranged(self):
        sections = [
            ctx.Section(ctx.SECTION_EDITABLE, _files(**{"e.py": 10_000}), 60_000),
            ctx.Section(ctx.SECTION_PROTECTED, _files(**{"p.py": 10_000}), 60_000),
        ]
        alloc = self._plan(sections)
        assert not alloc.dropped_any
        assert alloc.sections[ctx.SECTION_PROTECTED].chars == 10_000

    def test_an_editable_file_no_window_holds_is_refused_before_the_call(self):
        """The ticket's case: a 356K-char file against a 131K window. Dropping
        it designs around a file nobody can see; refusing names it."""
        sections = [
            ctx.Section(ctx.SECTION_EDITABLE,
                        _files(**{"small.py": 1_000, "orchestrator_daemon.py": 900_000}),
                        60_000),
            ctx.Section(ctx.SECTION_PROTECTED, _files(**{"p.py": 50_000}), 60_000),
        ]
        with pytest.raises(ctx.EditableFileTooLarge) as e:
            self._plan(sections)
        msg = str(e.value)
        assert "orchestrator_daemon.py" in msg and "small.py" not in msg
        assert "must edit" in msg and "200000" in msg and "big" in msg
        assert e.value.files == [("orchestrator_daemon.py", 300_000)]
        assert isinstance(e.value, ctx.PromptTooLarge), "parks like any oversized prompt"

    def test_the_refusal_comes_after_every_window_was_tried(self):
        """Escalation first (DEV-624): a file too big for the chosen agent but
        not for a candidate is served there, not refused."""
        sections = [ctx.Section(ctx.SECTION_EDITABLE, _files(**{"e.py": 120_000}), 60_000)]
        alloc = self._plan(sections, agent="small", candidates=("big",))
        assert alloc.agent == "big" and not alloc.dropped_any
        with pytest.raises(ctx.EditableFileTooLarge):
            self._plan(sections, agent="small", candidates=())

    def test_the_implementer_is_held_to_the_same_rule(self):
        sections = [ctx.Section(ctx.SECTION_EDITABLE, _files(**{"e.py": 900_000}), 60_000)]
        with pytest.raises(ctx.EditableFileTooLarge, match="implementer prompt"):
            self._plan(sections, role="implementer", agent="small", candidates=("big",))

    def test_a_dropped_reference_file_is_still_only_a_warning(self, caplog):
        import logging
        sections = [ctx.Section(ctx.SECTION_PROTECTED, _files(**{"p.py": 900_000}), 900_000)]
        with caplog.at_level(logging.WARNING, logger="orchestrator.context"):
            alloc = self._plan(sections)
        assert alloc.dropped(ctx.SECTION_PROTECTED) == ["p.py"]
        assert any("not shown" in r.getMessage() for r in caplog.records)


# ── the wiring: knobs, windows, reserves ─────────────────────────────────────

class TestDaemonWiring:
    def test_windows_come_from_the_agent_config(self):
        assert d._agent_ctx_limit("deep_implementer") == 262_144
        assert d._agent_ctx_limit("no_such_agent") is None

    def test_the_reasoning_reserve_is_read_from_the_server_args(self, monkeypatch):
        """An agent served with --reasoning-budget reserves it (DEV-616). No
        agent on today's roster carries the flag, so this registers one."""
        from coding_model_server.config import Config, _create_agent_config
        base = Config.AGENTS["dense_architect"]["model_config"]
        cfg = dict(base, server_extra_args=[
            *(base.get("server_extra_args") or []), "--reasoning-budget", "4096"])
        monkeypatch.setitem(Config.AGENTS, "test_budgeted", _create_agent_config(
            "t", "p", cfg))
        assert d._agent_reasoning_reserve("test_budgeted") == 4096

    def test_an_agent_without_a_reasoning_budget_reserves_nothing(self):
        assert d._agent_reasoning_reserve("deep_implementer") == 0
        assert d._agent_reasoning_reserve(None) == 0

    def test_raising_one_knob_no_longer_raises_the_other_section(self):
        """DEV-627's regression, now structural rather than a second constant:
        an operator raising the editable knob cannot buy the protected section
        room the window does not have."""
        sections = [
            ctx.Section(ctx.SECTION_EDITABLE, _files(**{"a.py": 100_000}),
                        400_000),
            ctx.Section(ctx.SECTION_PROTECTED, _files(**{"p.py": 400_000}),
                        400_000),
        ]
        alloc = d._prompt_budget("spec_t", "implementer", fixed_chars=0,
                                 completion_tokens=16_000, agent="implementer",
                                 sections=sections)
        assert alloc.needed_tokens <= d._agent_ctx_limit("implementer")
        assert not alloc.dropped(ctx.SECTION_EDITABLE)
        assert alloc.dropped(ctx.SECTION_PROTECTED)


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
        with mock.patch.object(settings, "PROTECTED_FILES_MAX_CHARS", 100):
            out = ex._render_reference_files([("src/big.py", "z" * 10_000)])
        assert "z" * 10_000 not in out
        assert "src/big.py" in out


# ── DEV-927: the refusal reaches the daemon's wrapper and parks ──────────────

class TestEditableRefusalWiring:
    def test_the_architect_wrapper_refuses_and_names_the_file(self):
        """The architect has no candidates (which architect runs is an eval
        decision), so a file its window cannot hold is refused here, before
        the call, instead of being omitted from the design."""
        sections = [
            ctx.Section(ctx.SECTION_EDITABLE,
                        _files(**{"src/coding_model_server/orchestrator_daemon.py": 900_000}),
                        settings.EXISTING_FILES_MAX_CHARS),
            ctx.Section(ctx.SECTION_PROTECTED, _files(**{"p.py": 50_000}),
                        settings.PROTECTED_FILES_MAX_CHARS),
        ]
        with pytest.raises(ctx.EditableFileTooLarge) as e:
            d._prompt_budget("spec_t", "architect", fixed_chars=20_000,
                             completion_tokens=8_000,
                             agent=settings.role_to_agent("architect"),
                             sections=sections)
        assert "orchestrator_daemon.py" in str(e.value)
        assert "architect prompt" in str(e.value)

    def test_the_refusal_parks_as_a_no_verdict_capped_at_one(self):
        from coding_model_autonomous.outcome import (
            FailureClass, Outcome, classify_exception)
        exc = ctx.EditableFileTooLarge("architect", [("big.py", 300_000)], 131_072, "arch")
        failure = classify_exception(exc, role="architect")
        assert failure.cls is FailureClass.PROMPT_TOO_LARGE
        assert failure.outcome is Outcome.NO_VERDICT
        assert failure.rotate is False
        assert failure.cap == 1
        assert "big.py" in failure.detail
