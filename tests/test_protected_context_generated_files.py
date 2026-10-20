"""Lockfiles and build output are protected by NAME, not shown as text.

A protected file is put in front of a role so it can use what the file
declares. A lockfile declares nothing, and it is routinely the largest file
in the repository: a 252,037-character package-lock.json, served as protected
context, fit no implementer window but the 262k one — so it alone routed every
call of the spec to the slowest model, which then re-read it on each call.

The file stays protected and stays named. Only its text is withheld, and it
is withheld BEFORE the budget is summed, because a file the budget merely
drops makes the dispatcher escalate to a bigger window to keep it.
"""
import pytest

from coding_model_autonomous import context as ctx
from coding_model_autonomous import languages
from coding_model_autonomous.executor import (
    ManifestEntry,
    _render_reference_files,
    build_architect_message,
    build_implementer_message,
    build_per_file_message,
    build_synthesis_message,
)
from coding_model_autonomous.languages import LanguagePack

LOCK = '{"lockfileVersion": 3, "packages": {' + '"x": {"version": "1.0.0"},' * 9000 + "}}"
SCAFFOLD = "export const COLUMNS = 30;\n"


# ── what counts ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "package-lock.json", "web/package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "pnpm-lock.yaml", "Package.resolved",
    "App.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
    "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "composer.lock",
    "Gemfile.lock", "go.sum", "dist/app.min.js", "dist/app.MIN.JS",
    "dist/app.js.map", "static/site.min.css", "static/site.css.map",
])
def test_lockfiles_and_build_output_are_machine_generated(path):
    assert languages.is_machine_generated(path)


@pytest.mark.parametrize("path", [
    # The hand-written manifests beside each lockfile: these say what the
    # code may import and where files go, and a role needs to read them.
    "package.json", "Package.swift", "Cargo.toml", "pyproject.toml",
    "requirements.txt", "go.mod", "Gemfile", "Pipfile", "composer.json",
    # Source, including generated source: it declares symbols that are in
    # scope, and hiding it is how a role re-declares one.
    "src/app.js", "src/app.ts", "Sources/Core/Field.swift",
    "Sources/Proto/messages.pb.swift", "src/schema.generated.ts",
    # Near misses on the name.
    "docs/package-lock.json.md", "src/yarn.lock.ts", "levels/level1.map",
    "src/minify.js", "lock.py",
])
def test_manifests_source_and_near_misses_are_not(path):
    assert not languages.is_machine_generated(path)


def test_each_ecosystems_lockfiles_are_its_packs_property():
    assert "package-lock.json" in languages.PACKS["javascript"].generated_files
    assert "Package.resolved" in languages.PACKS["swift"].generated_files
    assert "poetry.lock" in languages.PACKS["python"].generated_files
    # The neutral default: a pack that declares none withholds nothing.
    assert LanguagePack.generated_files == frozenset()
    assert LanguagePack.generated_suffixes == ()


def test_a_new_packs_lockfile_is_recognised_with_no_other_change(monkeypatch):
    class ZigPack(LanguagePack):
        name = "zig"
        generated_files = frozenset({"build.zig.zon.lock"})

    assert not languages.is_machine_generated("build.zig.zon.lock")
    monkeypatch.setitem(languages.PACKS, "zig", ZigPack())
    assert languages.is_machine_generated("vendor/build.zig.zon.lock")


# ── the budget never sees it ─────────────────────────────────────────────────

WINDOWS = {"small": 32_000, "big": 262_144}


def _plan(protected, *, editable=()):
    return ctx.plan_dispatch(
        "spec_t", role="implementer", fixed_chars=10_000,
        completion_tokens=4_000, agent="small", candidates=("small", "big"),
        window_of=WINDOWS.get,
        sections=[ctx.Section(ctx.SECTION_EDITABLE, list(editable), 60_000),
                  ctx.Section(ctx.SECTION_PROTECTED, list(protected), 60_000)])


def test_a_lockfile_does_not_route_the_prompt_to_the_largest_window():
    """The measured failure: 252K chars of lockfile is ~84K tokens, which the
    small window cannot hold and the big one can — so it used to move there."""
    protected = [("package-lock.json", LOCK), ("src/scaffold.ts", SCAFFOLD)]
    assert len(LOCK) > 200_000
    alloc = _plan(protected)
    assert alloc.agent == "small"
    assert alloc.files(ctx.SECTION_PROTECTED) == [("src/scaffold.ts", SCAFFOLD)]
    # The estimate the next attempt's routing reads is free of it too.
    assert alloc.prompt_chars == 10_000 + len(SCAFFOLD)


def test_withheld_is_not_dropped_but_is_still_named():
    alloc = _plan([("package-lock.json", LOCK), ("src/scaffold.ts", SCAFFOLD)])
    # Not a budget drop: nothing escalates or warns on its account.
    assert alloc.dropped(ctx.SECTION_PROTECTED) == []
    assert not alloc.dropped_any
    # But the render is still handed the path.
    assert alloc.not_shown(ctx.SECTION_PROTECTED) == ["package-lock.json"]
    assert "1 generated withheld" in alloc.describe()


def test_withheld_and_budget_dropped_are_both_named():
    huge = ("src/huge.ts", "x" * 2_000_000)      # fits no window
    alloc = _plan([("yarn.lock", LOCK), huge, ("src/scaffold.ts", SCAFFOLD)])
    assert alloc.dropped(ctx.SECTION_PROTECTED) == ["src/huge.ts"]
    assert alloc.not_shown(ctx.SECTION_PROTECTED) == ["yarn.lock", "src/huge.ts"]
    assert alloc.files(ctx.SECTION_PROTECTED) == [("src/scaffold.ts", SCAFFOLD)]


def test_a_lockfile_the_spec_modifies_is_still_shown():
    """Withholding is for read-only context. A lockfile in the editable
    section is there because the spec changes it, and a role that cannot see
    what it must edit invents it."""
    small_lock = '{"lockfileVersion": 3}'
    alloc = _plan([], editable=[("package-lock.json", small_lock)])
    assert alloc.files(ctx.SECTION_EDITABLE) == [("package-lock.json", small_lock)]
    assert alloc.not_shown(ctx.SECTION_EDITABLE) == []


def test_the_unbounded_path_withholds_too():
    alloc = ctx.allocate(
        [ctx.Section(ctx.SECTION_PROTECTED,
                     [("Cargo.lock", "y" * 500), ("src/lib.rs", "pub fn f() {}")],
                     60_000)],
        fixed_chars=0, window_tokens=None, completion_tokens=0)
    assert alloc.files(ctx.SECTION_PROTECTED) == [("src/lib.rs", "pub fn f() {}")]
    assert alloc.not_shown(ctx.SECTION_PROTECTED) == ["Cargo.lock"]


# ── the prompt still says the file exists and is off-limits ──────────────────

def test_the_render_names_a_withheld_lockfile_and_says_why():
    out = _render_reference_files([("src/scaffold.ts", SCAFFOLD)],
                                  omitted=["package-lock.json"])
    assert SCAFFOLD in out
    assert "package-lock.json" in out
    assert "off-limits" in out
    # Not blamed on the budget: that sentence is for files worth wishing for.
    assert "over the context budget" not in out
    assert "lockfiles and build output" in out


def test_the_render_keeps_the_two_reasons_apart():
    out = _render_reference_files(
        [], omitted=["package-lock.json", "src/huge.ts"])
    budget, generated = out.split("**Not shown**")[1:]
    assert "over the context budget" in budget and "src/huge.ts" in budget
    assert "package-lock.json" not in budget
    assert "package-lock.json" in generated and "src/huge.ts" not in generated


def test_an_unbudgeted_render_never_prints_a_lockfiles_text():
    """A caller that renders without the allocator hands the file over whole;
    the render withholds it itself, whatever the knob would have allowed."""
    tiny_lock = '{"lockfileVersion": 3, "marker": "LOCK-TEXT"}'
    out = _render_reference_files([("package-lock.json", tiny_lock),
                                   ("src/scaffold.ts", SCAFFOLD)])
    assert "LOCK-TEXT" not in out
    assert "package-lock.json" in out
    assert SCAFFOLD in out


def _user(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def test_every_role_that_gets_reference_context_is_told_about_the_lockfile():
    protected = [("package-lock.json", LOCK), ("src/scaffold.ts", SCAFFOLD)]
    alloc = _plan(protected)
    shown = alloc.files(ctx.SECTION_PROTECTED)
    named = alloc.not_shown(ctx.SECTION_PROTECTED)
    target = ManifestEntry("src/app.ts", "the app", "")
    prompts = {
        "architect": build_architect_message(
            "spec", reference_files=shown, omitted_reference=named),
        "implementer": build_implementer_message(
            "spec", "design", reference_files=shown, omitted_reference=named),
        "per-file": build_per_file_message(
            "spec", "design", [target], target, "none",
            reference_files=shown, omitted_reference=named),
        "synthesis": build_synthesis_message(
            "spec", "design", [], reference_files=shown,
            omitted_reference=named),
    }
    for role, messages in prompts.items():
        text = _user(messages)
        assert "package-lock.json" in text, role
        assert "lockfileVersion" not in text, role
        assert SCAFFOLD in text, role
        assert len(text) < 20_000, role
