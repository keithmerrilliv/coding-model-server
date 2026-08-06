"""Three fixes aimed at the same run: Centipede (DEV-102).

DEV-492 remainder — protected files are compiled into the target but were
invisible to every role, so run 5's design created a second `Field` and the
build died on `invalid redeclaration of 'Field'`. Read-only visibility is safe
by construction: the write path drops these paths regardless.

DEV-499 — those same protected paths were appearing IN the manifest, where each
file costs its own agent call. One of run 5's seven calls was spent generating
a file guaranteed to be discarded.

DEV-511 — Swift value-semantics errors are ~29% of every diagnostic this
pipeline has produced, recurring across every implementer in the rotation.
"""
import pytest

from coding_model_autonomous import executor
from coding_model_autonomous.executor import (
    SWIFT_VALUE_SEMANTICS,
    build_architect_message,
    build_implementer_message,
)


def _user(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def _system(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


# ── protected files as read-only context (DEV-492 remainder) ─────────────────

SCAFFOLD = ("public enum Field {\n"
            "    public static let columns = 30\n"
            "    public static let rows = 30\n"
            "}\n")


def test_architect_sees_protected_files():
    """The architect is where run 5's redeclaration actually originated."""
    text = _user(build_architect_message(
        "spec", reference_files=[("Sources/CentipedeCore/GameState.swift", SCAFFOLD)]))
    assert "GameState.swift" in text
    assert "public enum Field" in text


def test_protected_context_forbids_redeclaring_what_they_declare():
    """The specific failure being prevented, not a generic 'do not edit'."""
    text = _user(build_architect_message(
        "spec", reference_files=[("A/Scaffold.swift", SCAFFOLD)]))
    assert "re-declare" in text or "Declaring any of it a second time" in text
    assert "in scope already" in text


def test_protected_context_says_writes_are_discarded():
    text = _user(build_implementer_message(
        "spec", "design", reference_files=[("A/Scaffold.swift", SCAFFOLD)]))
    assert "discarded, not merged" in text
    assert "may NOT change" in text


def test_implementer_distinguishes_editable_from_read_only():
    """Both blocks can appear at once; conflating them would be worse than
    neither, since one says 'reproduce this' and the other 'never emit this'."""
    text = _user(build_implementer_message(
        "spec", "design",
        existing_files=[("Edit/Me.swift", "struct Editable {}")],
        reference_files=[("Dont/Touch.swift", "struct Protected {}")],
    ))
    assert "Current contents of files you must modify" in text
    assert "Existing files you may NOT change" in text
    assert text.index("must modify") < text.index("may NOT change")


def test_no_reference_files_leaves_prompts_unchanged():
    """Specs without protected_paths must be byte-identical to before."""
    assert (_user(build_architect_message("spec"))
            == _user(build_architect_message("spec", reference_files=[])))
    assert "may NOT change" not in _user(build_implementer_message("spec", "design"))


# ── manifest excludes undeliverable paths (DEV-499) ──────────────────────────

class _Entry:
    def __init__(self, path):
        self.path = path
        self.purpose = ""
        self.exports = ""

    def __repr__(self):
        return f"<{self.path}>"


def _spec_with(protected):
    import yaml as y

    class _S:
        id = "spec_test"
        normalized_yaml = y.safe_dump(
            {"test_strategy": {"repo": "centipede", "protected_paths": protected}})
    return _S()


def test_protected_paths_are_dropped_from_the_manifest():
    """Run 5's actual manifest: 7 entries, one of them Package.swift."""
    from coding_model_server import orchestrator_daemon as od
    entries = [_Entry(p) for p in [
        "Sources/CentipedeCore/Field.swift",
        "Sources/CentipedeCore/MushroomField.swift",
        "Sources/CentipedeCore/CentipedeChain.swift",
        "Sources/CentipedeCore/GameWorld.swift",
        "Tests/CentipedeCoreTests/SimulationTests.swift",
        "Package.swift",
    ]]
    kept = od._drop_undeliverable_manifest_entries(
        _spec_with(["Package.swift",
                    "Sources/CentipedeCore/GameState.swift"]), entries)
    assert [e.path for e in kept] == [
        "Sources/CentipedeCore/Field.swift",
        "Sources/CentipedeCore/MushroomField.swift",
        "Sources/CentipedeCore/CentipedeChain.swift",
        "Sources/CentipedeCore/GameWorld.swift",
        "Tests/CentipedeCoreTests/SimulationTests.swift",
    ]


def test_manifest_untouched_when_nothing_is_protected():
    from coding_model_server import orchestrator_daemon as od
    entries = [_Entry("a.swift"), _Entry("b.swift")]
    assert od._drop_undeliverable_manifest_entries(_spec_with([]), entries) == entries


def test_protected_drop_is_logged_not_silent(caplog):
    """_verify_manifest_workspace treats a manifest file missing from the
    workspace as an anomaly (DEV-106); this omission is deliberate and must be
    distinguishable from that."""
    from coding_model_server import orchestrator_daemon as od
    with caplog.at_level("INFO"):
        od._drop_undeliverable_manifest_entries(
            _spec_with(["Package.swift"]),
            [_Entry("a.swift"), _Entry("Package.swift")])
    assert any("Package.swift" in r.getMessage() for r in caplog.records)


# ── Swift value-semantics preamble (DEV-511) ─────────────────────────────────

@pytest.mark.parametrize("prompt_name", [
    "IMPLEMENTER_SYSTEM_PROMPT",
    "REVIEWER_SYSTEM_PROMPT",     # authors test files, which are Swift too
    "PER_FILE_SYSTEM_PROMPT",
    "SYNTHESIS_SYSTEM_PROMPT",
])
def test_every_code_writing_role_gets_the_preamble(prompt_name):
    assert SWIFT_VALUE_SEMANTICS in getattr(executor, prompt_name)


def test_preamble_covers_the_measured_error_classes():
    """Each bullet maps to a diagnostic in DEV-511's taxonomy."""
    p = SWIFT_VALUE_SEMANTICS
    assert "must be marked `mutating`" in p           # 'mutating' on value types
    assert "invalid on a `class`" in p                # 16 occurrences
    assert "`private(set) var`" in p                  # setter is inaccessible: 28
    assert "cannot be reassigned after init" in p     # 'let' constant: 24
    assert "memberwise" in p


def test_preamble_rides_the_cached_system_prefix_not_the_user_turn():
    """DEV-409: a stable system prompt is cache-friendly; interpolating this
    into the user message would cost a cache miss on all 26 per-file calls."""
    msgs = build_implementer_message("spec", "design")
    assert "value semantics" in _system(msgs)
    assert "value semantics" not in _user(msgs)


def test_preamble_stays_short_because_it_is_multiplied():
    """Prepended to every per-file call; length is multiplied by the manifest
    size (26 on one observed build)."""
    assert len(SWIFT_VALUE_SEMANTICS.splitlines()) <= 20
    assert len(SWIFT_VALUE_SEMANTICS) < 1200
