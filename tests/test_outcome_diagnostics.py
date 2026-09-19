

# ── DEV-755: colourised swiftc output ────────────────────────────────────────
#
# Verbatim from run 45 (spec_2ab81c4b, retry_2). swiftc puts the SGR escape
# BETWEEN the location and the keyword, so a regex needing a literal
# ": error: " never fires and a failing build reports zero diagnostics.
# xcodebuild output is not coloured, which is why this survived until a
# swift_test run met it.
_ANSI_SWIFT_ERROR = (
    "/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_2ab81c4b-d7b"
    "844f6/Sources/CentipedeCore/Game.swift:109:38: \x1b[1;31merror: \x1b[1;39m"
    "value of optional type 'Spider?' must be unwrapped to refer to member "
    "'position' of wrapped base type 'Spider'\x1b[0;0m\n"
    "error: fatalError\n"
    "error: Build failed\n"
)


def test_colourised_swift_diagnostic_is_attributed():
    """An ANSI-coloured swiftc error still counts, and counts once."""
    from coding_model_autonomous.outcome import attributed_diagnostics
    diags = attributed_diagnostics(_ANSI_SWIFT_ERROR)
    assert len(diags) == 1, f"expected the one attributed error, got {diags!r}"
    # The bare driver lines (fatalError, Build failed) name no file:line and
    # must stay excluded — that exclusion is deliberate and predates this fix.
    assert "fatalError" not in diags[0]


def test_colourised_message_carries_no_escapes():
    """Escapes must not reach the message, or the same defect compares unequal.

    The failure IDENTITY is built from these strings (DEV-631). If one run's
    'value of optional type' carries \\x1b[1;39m and another's does not, they
    are different defects to every consumer downstream.
    """
    from coding_model_autonomous.outcome import diagnostic_messages
    msg = next(iter(diagnostic_messages(_ANSI_SWIFT_ERROR)))
    assert "\x1b" not in msg
    assert msg.startswith("value of optional type 'Spider?'")


def test_uncoloured_output_is_unchanged_by_the_strip():
    """The xcodebuild path must read exactly as it did before DEV-755."""
    from coding_model_autonomous.outcome import attributed_diagnostics
    plain = ("/w/Sources/A.swift:12:5: error: cannot find type 'ScenePhase' in scope\n"
             "/w/Sources/A.swift:14:9: error: cannot find type 'ScenePhase' in scope\n")
    diags = attributed_diagnostics(plain)
    assert len(diags) == 2                      # occurrences, not a set (DEV-541)
    assert len(set(diags)) == 1
