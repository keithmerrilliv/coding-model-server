"""DEV-714: the architect's read-only tool loop.

The acceptance case the ticket names is the last class here: a design pass
whose modification set is short a file resolves the missing symbol by reading
it, instead of inventing an API the way DEV-698's spec_ba4c353e did.
"""

import pytest

from coding_model_autonomous import architect_tools as at


# ── the protocol ────────────────────────────────────────────────────────────

class TestParsing:
    def test_reads_a_single_marker(self):
        assert at.parse_tool_markers(
            "<<<READ_FILE>>>ElectricSheep/Foo.swift"
        ) == [("READ_FILE", "ElectricSheep/Foo.swift")]

    def test_keeps_order_and_repeats(self):
        got = at.parse_tool_markers(
            "think\n<<<READ_FILE>>>a.swift\nmore\n<<<READ_FILE>>>b.swift\n")
        assert got == [("READ_FILE", "a.swift"), ("READ_FILE", "b.swift")]

    def test_tolerates_lowercase_and_padding(self):
        assert at.parse_tool_markers("<<<read_file>>>   a.swift  ") == [
            ("READ_FILE", "a.swift")]

    def test_ignores_prose_that_merely_mentions_the_tool(self):
        assert at.parse_tool_markers("I could use READ_FILE here.") == []

    def test_a_design_with_no_markers_wants_no_tools(self):
        assert not at.wants_tools("## Design\n\nAdd a cursor.")

    def test_markers_are_stripped_from_the_final_design(self):
        # The testability checker reads spans out of the design; a surviving
        # marker would be scored as content.
        out = at.strip_tool_markers("## Design\n<<<READ_FILE>>>a.swift\nbody")
        assert "READ_FILE" not in out
        assert "## Design" in out and "body" in out

    def test_empty_and_none_are_safe(self):
        assert at.parse_tool_markers("") == []
        assert at.parse_tool_markers(None) == []
        assert at.strip_tool_markers(None) == ""


# ── budget ──────────────────────────────────────────────────────────────────

class TestBudget:
    def test_starts_unspent(self):
        b = at.ToolBudget()
        assert not b.exhausted()
        assert b.rounds_left == at.DEFAULT_MAX_ROUNDS

    def test_rounds_exhaust(self):
        b = at.ToolBudget(max_rounds=1)
        at.resolve_round([("READ_FILE", "a")], read=_read({"a": "x"}), budget=b)
        assert b.exhausted()

    def test_chars_exhaust_independently_of_rounds(self):
        # A round cap alone does not bound cost: one round can ask for forty
        # files. The character budget is the real ceiling.
        b = at.ToolBudget(max_rounds=9, max_chars=10)
        at.resolve_round([("READ_FILE", "a")], read=_read({"a": "x" * 500}), budget=b)
        assert b.rounds_left == 8
        assert b.exhausted()

    def test_counters_never_go_negative(self):
        b = at.ToolBudget(max_rounds=1, max_chars=5)
        at.resolve_round([("READ_FILE", "a")], read=_read({"a": "y" * 99}), budget=b)
        at.resolve_round([("READ_FILE", "b")], read=_read({"b": "y" * 99}), budget=b)
        assert b.chars_left == 0 and b.rounds_left == 0


def _read(files: dict, problems=()):
    """A stand-in for the bound reader SpecContext.reader() hands out."""
    def read(paths):
        return ([(p, files[p]) for p in paths if p in files],
                [*problems, *(f"{p}: not found" for p in paths
                              if p not in files)])
    return read


# ── resolution ──────────────────────────────────────────────────────────────

class TestReadFile:
    def test_serves_the_content(self):
        b = at.ToolBudget()
        out = at.resolve_round([("READ_FILE", "F.swift")],
                               read=_read({"F.swift": "func spawnWaveChain()"}), budget=b)
        assert "func spawnWaveChain()" in out
        assert b.chars_used > 0

    def test_batches_one_round_into_one_runner_call(self):
        calls = []

        def read(paths):
            calls.append(list(paths))
            return ([(p, "x") for p in paths], [])

        at.resolve_round([("READ_FILE", "a"), ("READ_FILE", "b"),
                          ("READ_FILE", "c")],
                         read=read, budget=at.ToolBudget())
        assert calls == [["a", "b", "c"]]

    def test_a_missing_path_is_reported_not_silently_dropped(self):
        # DEV-630: absent is not unknown. A model told nothing about a path it
        # asked for will ask again and spend the round.
        out = at.resolve_round([("READ_FILE", "nope.swift")],
                               read=_read({}),
                               budget=at.ToolBudget())
        assert "COULD NOT READ" in out and "nope.swift" in out

    def test_content_is_truncated_at_the_budget_not_dropped(self):
        b = at.ToolBudget(max_chars=20)
        out = at.resolve_round([("READ_FILE", "big")],
                               read=_read({"big": "z" * 5000}), budget=b)
        assert "TRUNCATED" in out
        assert len(out) < 1000

    def test_later_files_in_a_round_say_the_budget_was_spent(self):
        b = at.ToolBudget(max_chars=10)
        out = at.resolve_round([("READ_FILE", "a"), ("READ_FILE", "b")],
                               read=_read({"a": "q" * 50, "b": "q" * 50}), budget=b)
        assert "BUDGET SPENT" in out

    def test_a_fetch_that_raises_degrades_to_a_message(self):
        # A runner outage mid-design must not take the attempt down; the
        # architect still has its served context and can design from it.
        def boom(paths):
            raise ConnectionResetError("ECONNRESET")

        out = at.resolve_round([("READ_FILE", "a")], read=boom,
                               budget=at.ToolBudget())
        assert "COULD NOT READ" in out and "ECONNRESET" in out

    def test_no_repo_says_so_rather_than_failing(self):
        # The context stage hands out None when the spec has no repository.
        out = at.resolve_round([("READ_FILE", "a")], read=None,
                               budget=at.ToolBudget())
        assert "names no repository" in out

    def test_an_empty_path_is_not_fetched(self):
        called = []

        def read(paths):
            called.append(paths)
            return ([], [])

        at.resolve_round([("READ_FILE", "")], read=read,
                         budget=at.ToolBudget())
        assert called == []


class TestRefusals:
    @pytest.mark.parametrize("tool", at.UNAVAILABLE_TOOLS)
    def test_unavailable_tools_are_answered_not_ignored(self, tool):
        # Silence is the worst response: the model asks again and spends the
        # round. Naming the limit converts a wasted round into a useful one.
        out = at.resolve_round([(tool, "ElectricSheep/**")], read=_read({}),
                               budget=at.ToolBudget())
        assert "UNAVAILABLE" in out
        assert "READ_FILE" in out          # told what it CAN use
        assert "Do not ask for this again" in out

    @pytest.mark.parametrize("tool", at.REFUSED_TOOLS)
    def test_write_tools_are_refused_with_a_reason(self, tool):
        out = at.resolve_round([(tool, "a.swift")], read=_read({}),
                               budget=at.ToolBudget())
        assert "REFUSED" in out and "DESIGN" in out

    def test_a_refused_tool_never_reaches_the_runner(self):
        calls = []

        def read(paths):
            calls.append(paths)
            return ([], [])

        at.resolve_round([("WRITE_FILE", "a"), ("GLOB", "*.swift")],
                         read=read, budget=at.ToolBudget())
        assert calls == []

    def test_refusals_still_cost_a_round(self):
        # Otherwise a model that only ever emits GLOB loops forever.
        b = at.ToolBudget(max_rounds=2)
        at.resolve_round([("GLOB", "*")], read=_read({}), budget=b)
        assert b.rounds_used == 1


class TestFraming:
    def test_says_how_much_is_left(self):
        out = at.resolve_round([("READ_FILE", "a")], read=_read({"a": "x"}),
                               budget=at.ToolBudget(max_rounds=3))
        assert "round 1/3" in out

    def test_the_last_round_demands_the_final_design(self):
        b = at.ToolBudget(max_rounds=1)
        out = at.resolve_round([("READ_FILE", "a")], read=_read({"a": "x"}), budget=b)
        assert "SPENT" in out and "NO tool markers" in out

    def test_a_mid_loop_round_invites_more(self):
        out = at.resolve_round([("READ_FILE", "a")], read=_read({"a": "x"}),
                               budget=at.ToolBudget(max_rounds=3))
        assert "Continue." in out


class TestSummary:
    def test_records_what_was_asked_for(self):
        b = at.ToolBudget()
        at.resolve_round([("READ_FILE", "a.swift"), ("GLOB", "*.swift")],
                         read=_read({"a.swift": "x"}), budget=b)
        s = at.summary(b)
        assert s["tool_rounds"] == 1
        assert s["tool_calls"] == ["READ_FILE a.swift", "GLOB *.swift"]
        assert s["tool_chars"] == 1
        assert s["tool_budget_spent"] is False

    def test_an_untouched_budget_summarises_to_zero(self):
        s = at.summary(at.ToolBudget())
        assert s == {"tool_rounds": 0, "tool_calls": [], "tool_chars": 0,
                     "tool_budget_spent": False}


# ── the acceptance case ─────────────────────────────────────────────────────

class TestDev698Regression:
    """spec_ba4c353e: the design called `spawnWaveChain()` because the file
    defining it was never served. Five attempts died on it. With the loop, the
    architect asks and gets the real signature."""

    SERVED = "class Bridge {\n  func update() { spawnWaveChain() }\n}"
    UNSERVED = ("extension Bridge {\n"
                "  func spawnWaveChain(count: Int, seed: UInt64) {}\n}")

    def test_the_missing_definition_is_reachable_in_one_round(self):
        b = at.ToolBudget()
        out = at.resolve_round(
            [("READ_FILE", "ElectricSheep/Bridge+Waves.swift")],
            read=_read({"ElectricSheep/Bridge+Waves.swift": self.UNSERVED}),
            budget=b)
        # The real signature takes two arguments; designing blind produced a
        # no-argument call that could not compile.
        assert "spawnWaveChain(count: Int, seed: UInt64)" in out
        assert b.rounds_used == 1

    def test_the_whole_exchange_fits_the_default_budget(self):
        b = at.ToolBudget()
        at.resolve_round([("READ_FILE", "a"), ("READ_FILE", "b")],
                         read=_read({"a": self.SERVED, "b": self.UNSERVED}), budget=b)
        assert not b.exhausted()


class TestIsToolRequest:
    """A finished design is never re-read as a request (the loop-forever bug)."""

    def test_markers_with_no_design_are_a_request(self):
        assert at.is_tool_request("<<<READ_FILE>>>a.swift")

    def test_a_design_block_settles_it_even_with_a_marker(self):
        # A design describing the implementer's own protocol must not be fed
        # back into the tool stage.
        text = ("<<<DESIGN>>>\n## Files\nThe implementer emits "
                "<<<WRITE_FILE>>>a.swift here.\n<<<END>>>")
        assert not at.is_tool_request(text)

    def test_a_plain_design_is_not_a_request(self):
        assert not at.is_tool_request("<<<DESIGN>>>\nbody\n<<<END>>>")

    def test_empty_is_not_a_request(self):
        assert not at.is_tool_request("")
        assert not at.is_tool_request(None)

    def test_the_design_delimiters_survive_stripping(self):
        text = "<<<DESIGN>>>\nbody\n<<<END>>>\n<<<READ_FILE>>>a"
        out = at.strip_tool_markers(text)
        assert "<<<DESIGN>>>" in out and "<<<END>>>" in out
        assert "READ_FILE" not in out
