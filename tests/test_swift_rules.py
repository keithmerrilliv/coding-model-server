"""DEV-764 / DEV-767: Swift rules, located diagnostics, and cite-or-refuse.

The two incidents these tests pin:
* run 47 — one diagnostic at HalluRenderer.swift:199; the repair changed four
  OTHER lines and left 199 alone (1 -> 6).
* run 48 — 14 diagnostics, all in the test file; the repair rewrote the
  renderer and never touched the test file (14 -> 14).
"""
from coding_model_autonomous import swift_rules as sr
from coding_model_autonomous import executor

WT = "/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_a8b7c3e5-fef2fc96"
ESC = "\x1b[1;31m"
RESET = "\x1b[0;0m"

RUN48 = (
    f"{WT}/Tests/CentipedeRenderTests/OffscreenRendererTests.swift:26:24: "
    f"{ESC}error: {RESET}errors thrown from here are not handled\n"
    f"{WT}/Tests/CentipedeRenderTests/OffscreenRendererTests.swift:27:21: "
    f"error: errors thrown from here are not handled\n"
    f"{WT}/Tests/CentipedeRenderTests/OffscreenRendererTests.swift:83:17: "
    f"error: generic struct 'Set' requires that 'RGBA' conform to 'Hashable'\n"
    "error: fatalError\n"
)
ARTIFACTS = ["Package.swift", "Sources/CentipedeRender/OffscreenRenderer.swift",
             "Tests/CentipedeRenderTests/OffscreenRendererTests.swift"]


# ── rules paragraph ──────────────────────────────────────────────────────────

def test_rules_render_only_for_swift():
    assert sr.render_swift_rules([]) == ""
    assert sr.render_swift_rules(["src/a.py", "tests/test_a.py"]) == ""
    out = sr.render_swift_rules(["Sources/A.swift"])
    assert "Self." in out and "throws" in out and "@MainActor" in out


def test_implementer_prompt_carries_the_rules_for_swift_only():
    swift = executor.build_implementer_message(
        "# spec", "# design", new_files=["Sources/A.swift"])
    py = executor.build_implementer_message(
        "# spec", "# design", new_files=["src/pkg/a.py"])
    assert "Swift rules" in swift[-1]["content"]
    assert "Swift rules" not in py[-1]["content"]


# ── located diagnostics ──────────────────────────────────────────────────────

def test_located_diagnostics_strip_ansi_map_to_artifacts_and_dedupe():
    diags = sr.located_diagnostics(RUN48, ARTIFACTS)
    assert [(d.artifact, d.line) for d in diags] == [
        ("Tests/CentipedeRenderTests/OffscreenRendererTests.swift", 26),
        ("Tests/CentipedeRenderTests/OffscreenRendererTests.swift", 27),
        ("Tests/CentipedeRenderTests/OffscreenRendererTests.swift", 83),
    ]
    assert diags[0].message == "errors thrown from here are not handled"
    # the bare driver line is not located
    assert all("fatalError" not in d.message for d in diags)


def test_suffix_mapping_prefers_the_longest_match():
    arts = ["Tests/X.swift", "Sources/Tests/X.swift"]
    assert sr.map_to_artifact("/w/abc/Sources/Tests/X.swift", arts) == "Sources/Tests/X.swift"
    assert sr.map_to_artifact("/w/abc/Tests/X.swift", arts) == "Tests/X.swift"
    assert sr.map_to_artifact("/w/abc/Other/X.swift", arts) is None


def test_fix_hints_for_the_week_s_diagnostic_classes():
    assert "Self.maxFramesInFlight" in sr.fix_hint(
        "static member 'maxFramesInFlight' cannot be used on instance of type 'HalluRenderer'")
    assert "throws" in sr.fix_hint("errors thrown from here are not handled")
    assert "@MainActor" in sr.fix_hint(
        "call to main actor-isolated initializer 'init()' in a synchronous nonisolated context")
    assert sr.fix_hint("generic struct 'Set' requires that 'RGBA' conform to 'Hashable'") \
        == "add `: Hashable` to the declaration of `RGBA`"
    assert sr.fix_hint("something the table does not know") is None


def test_cited_section_lists_locations_with_hints_and_the_rule():
    diags = sr.located_diagnostics(RUN48, ARTIFACTS)
    out = sr.render_cited_diagnostics(diags)
    assert "OffscreenRendererTests.swift:26" in out
    assert "→ fix: declare the enclosing function `throws`" in out
    assert "not built at all" in out
    assert sr.render_cited_diagnostics([]) == ""


def test_repair_prompt_carries_cited_section_and_rules_on_build_failure():
    diags = sr.located_diagnostics(RUN48, ARTIFACTS)
    files = [(p, "// x") for p in ARTIFACTS]
    msg = executor.build_synthesis_repair_message(
        "# spec", "# design", files, RUN48,
        build_diagnostic="error: x", cited_diagnostics=diags)
    text = msg[-1]["content"]
    assert "## Cited locations" in text and "## Swift rules" in text
    # near-miss prompt stays as it was
    near = executor.build_synthesis_repair_message(
        "# spec", "# design", files, "18 passed, 2 failed")
    assert "## Cited locations" not in near[-1]["content"]


# ── cite-or-refuse ───────────────────────────────────────────────────────────

TEST_FILE_BEFORE = "\n".join(
    ["import Testing"] + [f"line {n}" for n in range(2, 26)]
    + ["    @Test func criterion3() {", "        let r = try #require(X())",
       "        let f = try #require(r.render())"] + [f"line {n}" for n in range(29, 90)]) + "\n"
RENDERER_BEFORE = "public final class OffscreenRenderer {\n    let x = 1\n}\n"


def _diags():
    return sr.located_diagnostics(RUN48, ARTIFACTS)


def test_run48_shape_is_refused_uncited_file_dropped():
    """The repair rewrote the renderer; every diagnostic was in the tests."""
    res = sr.filter_repair_to_cited(
        [("Sources/CentipedeRender/OffscreenRenderer.swift",
          RENDERER_BEFORE.replace("let x = 1", "let x = Self.y"))],
        {"Sources/CentipedeRender/OffscreenRenderer.swift": RENDERER_BEFORE},
        _diags())
    assert res.applied
    assert res.dropped == ["Sources/CentipedeRender/OffscreenRenderer.swift"]
    assert res.kept == []
    assert res.refuse()


def test_run47_shape_is_refused_cited_line_untouched():
    """The cited line stays identical; four other lines change."""
    d = sr.located_diagnostics(
        "/w/x/HalluRenderer.swift:5:41: error: static member 'k' cannot be used on instance",
        ["HalluRenderer.swift"])
    before = "\n".join(f"l{n}" for n in range(1, 12)) + "\n"
    after = before.replace("l1\n", "l1x\n").replace("l9\n", "l9x\n").replace("l11\n", "l11x\n")
    res = sr.filter_repair_to_cited([("HalluRenderer.swift", after)],
                                    {"HalluRenderer.swift": before}, d)
    assert res.applied and res.kept and res.untouched == ["HalluRenderer.swift"]
    assert res.refuse()


def test_a_fix_on_the_line_above_the_citation_counts():
    """`throws` lands on the `func` line, one above the cited `try`."""
    after = TEST_FILE_BEFORE.replace("@Test func criterion3() {",
                                     "@Test func criterion3() throws {")
    res = sr.filter_repair_to_cited(
        [("Tests/CentipedeRenderTests/OffscreenRendererTests.swift", after)],
        {"Tests/CentipedeRenderTests/OffscreenRendererTests.swift": TEST_FILE_BEFORE},
        _diags())
    assert res.applied and res.touched_cited_line and not res.refuse()
    assert res.kept[0][0].endswith("OffscreenRendererTests.swift")


def test_no_citations_means_passthrough():
    files = [("a.swift", "x"), ("b.swift", "y")]
    res = sr.filter_repair_to_cited(files, {"a.swift": "q", "b.swift": "r"}, [])
    assert not res.applied and res.kept == files and not res.refuse()


def test_cited_file_absent_from_workspace_is_kept():
    d = sr.located_diagnostics("/w/x/New.swift:3:1: error: cannot find 'q' in scope",
                               ["New.swift"])
    res = sr.filter_repair_to_cited([("New.swift", "fixed")], {"New.swift": None}, d)
    assert res.kept == [("New.swift", "fixed")] and not res.refuse()


# ── DEV-778: the hint table pinned on the real diagnostic text, both prompts ─

RUN49_NIL = ("/Users/km4/w/spec_4baf2650/Sources/CentipedeRender/OffscreenRenderer.swift:158:126: "
             "\x1b[1;31merror: \x1b[1;39m'nil' is not compatible with expected argument type "
             "'MTLResourceOptions'\x1b[0;0m\n")
RUN44_ACTOR = ("/Users/km4/w/spec_86fec6ac/ElectricSheepTests/AudioLifecycleTests.swift:48:18: error: "
               "call to main actor-isolated initializer 'init()' in a synchronous nonisolated context\n"
               "/Users/km4/w/spec_86fec6ac/ElectricSheep/AudioManager.swift:19:25: error: "
               "main actor-isolated property 'notificationObservers' can not be referenced from a "
               "nonisolated context\n")
RUN47_STATIC = ("/Users/km4/w/spec_ffe89fdd/ElectricSheep/HalluRenderer.swift:191:41: error: "
                "static member 'maxFramesInFlight' cannot be used on instance of type 'HalluRenderer'\n")
RUN10_REDECL = "/w/Sources/Game/Player.swift:2:6: error: invalid redeclaration of 'Direction'\n"
RUN42_SCOPE = "/w/ElectricSheep/App.swift:30:9: error: cannot find 'MetricsParticleBridge' in scope\n"


def test_every_row_matches_its_run_s_text_and_only_its_row():
    cases = {
        RUN49_NIL: ("pass a `MTLResourceOptions` value", "not optional"),
        RUN47_STATIC: ("Self.maxFramesInFlight", "do not remove"),
        RUN10_REDECL: ("duplicate declaration of `Direction`", "never rename"),
        RUN42_SCOPE: ("`MetricsParticleBridge` is not declared", "do NOT invent"),
    }
    for text, (a, b) in cases.items():
        d = sr.located_diagnostics(text)
        assert len(d) == 1, text
        hint = sr.fix_hint(d[0].message)
        assert hint and a in hint and b in hint, (text, hint)
    actor = sr.located_diagnostics(RUN44_ACTOR)
    assert len(actor) == 2
    for d in actor:
        hint = sr.fix_hint(d.message)
        assert "@MainActor" in hint and "async throws" in hint and "never" in hint
    assert sr.fix_hint("errors thrown from here are not handled").startswith("declare the enclosing function `throws`")


def test_retry_mode_renders_only_present_rows_and_a_softer_rule():
    diags = sr.located_diagnostics(RUN49_NIL + RUN47_STATIC)
    out = sr.render_cited_diagnostics(diags, repair=False)
    assert out.count("→ fix:") == 2
    assert "MTLResourceOptions" in out and "Self.maxFramesInFlight" in out
    assert "@MainActor" not in out and "throws" not in out       # absent classes stay absent
    assert "not built at all" not in out                         # the repair rule
    assert "ONE edit that diagnostic asks for" in out
    assert sr.render_cited_diagnostics([], repair=False) == ""


def test_implementer_build_failure_note_carries_hints_and_a_located_headline():
    """DEV-778 + DEV-768: run 48's shape — the raw output is coloured, the bare
    `error: SwiftCompile … failed` line comes first, and the real
    `path:line:col: error:` sits further down."""
    import coding_model_server.orchestrator_daemon as d
    raw = ("error: SwiftCompile normal arm64 /w/Sources/CentipedeRender/OffscreenRenderer.swift "
           "failed with a nonzero exit code\n" + RUN49_NIL + "error: Build failed\n")
    note = d._build_failure_feedback(
        raw, "SwiftCompile normal arm64 … failed with a nonzero exit code",
        "swift_test", ["Sources/CentipedeRender/OffscreenRenderer.swift"])
    assert "First compiler diagnostic:\n\n    Sources/CentipedeRender/OffscreenRenderer.swift:158: error: 'nil'" in note
    assert "## Cited locations" in note
    assert "→ fix: the parameter is not optional" in note
    assert "No located error was reported at all" not in note   # the DEV-768 false banner
    assert "\x1b[" not in note                                    # nothing coloured survives
    # No located diagnostic at all: the headline falls back to build_reason and
    # the cited section is absent — a non-Swift or emit-module-only failure.
    bare = d._build_failure_feedback("error: emit-module command failed\n",
                                     "emit-module command failed", "swift_test", [])
    assert "First compiler diagnostic:\n\n    emit-module command failed" in bare
    assert "## Cited locations" not in bare
    assert "No located error was reported at all" in bare
