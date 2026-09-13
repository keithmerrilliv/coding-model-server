"""DEV-539: a citation has a position — prose that names a file to rule it
out must not put that file up for regeneration.

Run 8 of the Centipede spec: the rejection said "do not go looking in
`Mushroom.swift` or `CentipedeChain.swift`", all three diagnostics were in
World.swift, and the targeted retry regenerated all three. CentipedeChain
had compiled; it came back missing its initialiser. DEV-434's tests pin the
other direction (`resolver.ts:129` must still resolve by basename).
"""
import logging

import coding_model_server.orchestrator_daemon as d

KNOWN = {"Sources/CentipedeCore/World.swift",
         "Sources/CentipedeCore/Mushroom.swift",
         "Sources/CentipedeCore/CentipedeChain.swift",
         "Sources/CentipedeCore/GameState.swift"}
WORLD, MUSHROOM, CHAIN, STATE = sorted(KNOWN, key=lambda p: ["W", "M", "C", "G"].index(p.split("/")[-1][0]))

RUN_8 = """## Build failed

```
/tmp/wt/Sources/CentipedeCore/World.swift:238:20: error: cannot find 'hitIndex' in scope
/tmp/wt/Sources/CentipedeCore/World.swift:241:9: error: value of type 'Segment' has no member 'resolve'
World.swift:250:5: error: missing return in instance method
```

Both defects are in the hit-resolution path, and neither is a missing
declaration elsewhere — do not go looking in `Mushroom.swift` or
`CentipedeChain.swift` for something to add.
"""


class TestRun8Replay:
    def test_only_the_file_the_compiler_named_is_cited(self):
        assert d._parse_cited_paths(RUN_8, KNOWN) == {WORLD}

    def test_provenance_says_why(self):
        assert d._cite_paths(RUN_8, KNOWN) == {WORLD: "diagnostic"}

    def test_the_log_shows_the_ruled_out_files_were_seen_and_skipped(self, caplog):
        with caplog.at_level(logging.INFO):
            d._log_citations("spec_x", RUN_8, d._cite_paths(RUN_8, KNOWN), KNOWN)
        text = caplog.text
        assert f"{WORLD} (diagnostic)" in text
        assert "NOT cited" in text and MUSHROOM in text and CHAIN in text
        assert STATE not in text  # never mentioned, never logged


class TestTiers:
    def test_dev434_basename_with_line_still_resolves(self):
        known = {"App/server/resolver.ts", "App/client/probe.ts", "shared/t.ts"}
        notes = ("### Verdict Evidence\n- resolver.ts:129 - as any\n"
                 "- App/client/probe.ts:7 - any\nunrelated note about anything else")
        assert d._cite_paths(notes, known) == {
            "App/server/resolver.ts": "diagnostic", "App/client/probe.ts": "diagnostic"}

    def test_a_fenced_block_counts_when_no_diagnostic_position_exists(self):
        notes = "The failing test output:\n```\nFAILED tests/test_x.py - assert 1 == 2\n```\nfix it"
        known = {"tests/test_x.py", "src/x.py"}
        assert d._cite_paths(notes, known) == {"tests/test_x.py": "fenced"}

    def test_prose_is_the_last_resort(self):
        """A human review with no build output: the pre-DEV-539 rule, and
        logged as weak. "cli.py is fine" cites cli.py here — the alternative
        when nothing else cites is regenerating every file, cli.py included,
        so the prose tier costs nothing the fallback would not."""
        known = {"src/pkg/engine.py", "src/pkg/util.py", "src/pkg/cli.py", "src/pkg/io.py"}
        notes = ("Please fix src/pkg/engine.py — the loop is off by one. "
                 "`util.py` needs the same guard. cli.py is fine.")
        assert d._cite_paths(notes, known) == {
            "src/pkg/engine.py": "prose", "src/pkg/util.py": "prose",
            "src/pkg/cli.py": "prose"}

    def test_prose_does_not_count_once_anything_positional_exists(self):
        known = {"src/pkg/engine.py", "src/pkg/util.py"}
        notes = "src/pkg/engine.py:10: error: boom\nalso check `util.py`"
        assert d._cite_paths(notes, known) == {"src/pkg/engine.py": "diagnostic"}

    def test_a_fenced_block_also_outranks_prose(self):
        known = {"src/pkg/engine.py", "src/pkg/util.py"}
        notes = "```\nTraceback in src/pkg/engine.py\n```\nutil.py is unrelated"
        assert d._cite_paths(notes, known) == {"src/pkg/engine.py": "fenced"}

    def test_a_basename_never_matches_inside_another_token(self):
        known = {"shared/t.ts"}
        assert d._cite_paths("unrelated note about anything else", known) == {}

    def test_nothing_known_nothing_cited(self):
        assert d._cite_paths(RUN_8, set()) == {}
        assert d._cite_paths("", KNOWN) == {}
