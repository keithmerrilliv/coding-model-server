"""Swift's prompt rules: the standing paragraph, the fix hints, and the
default-isolation rule (DEV-764, DEV-767, DEV-778, DEV-784).

Runs 44-48 (2026-09-19/20) each ended a few one-token Swift slips from
delivery, and the repair round could not turn the compiler's diagnostic into
the edit it named:

* run 47: ``static member 'maxFramesInFlight' cannot be used on instance`` at
  one line; the repair left that line alone and REMOVED qualifiers from four
  others (1 -> 6). Retry 1, synthesis and repair all made the same slip.
* run 48: 14 diagnostics, every one in the test file (``try #require`` inside
  ``@Test func`` not declared ``throws``); the repair rewrote the renderer and
  never touched the test file (14 -> 14).

This is the DEV-644 treatment for Swift. DEV-644 fixed "the feedback named
the module, so each agent fixed the module rather than the import" with a
standing paragraph rendered into every Python prompt. Here:

1. :data:`SWIFT_RULES` — the standing paragraph, rendered into the
   implementer, synthesis and repair prompts whenever the file set has Swift.
2. :func:`fix_hint` — what a Swift diagnostic MEANS to change, rendered
   beside each cited location (``citations``).
3. :func:`render_default_isolation_rule` — the rules a default-MainActor
   target needs, when the operator's test_strategy declares one.

Locating diagnostics and refusing uncited repairs are language-neutral, and
live in ``diagnostics`` and ``citations``. Everything here is pure.
"""
from __future__ import annotations

import re

# ── 1. The standing paragraph ────────────────────────────────────────────────

# FROZEN as of DEV-778 (2026-09-20): a new diagnostic class goes into
# _FIX_HINTS (reactive, rendered only when the diagnostic is present) and
# prechecks (detected before the build) — NOT into this paragraph,
# which every Swift prompt pays for whether or not the class is at issue.
SWIFT_RULES = (
    "## Swift rules — MANDATORY\n\n"
    "Each of these has cost a full attempt; the compiler's message for each is "
    "quoted so you recognise it and apply the ONE edit it asks for.\n\n"
    "1. A `static` member used inside an instance method, initializer or "
    "computed property MUST be qualified: `Self.name` or `TypeName.name`. "
    "The diagnostic `static member 'X' cannot be used on instance of type 'T'` "
    "means ADD `Self.` on that line. It never means remove qualifiers "
    "elsewhere.\n"
    "2. A Swift Testing `@Test func` whose body uses `try` — including "
    "`try #require(...)` — MUST be declared `throws`: `@Test func name() "
    "throws {`. The diagnostic `errors thrown from here are not handled` "
    "means add `throws` to the enclosing function.\n"
    "3. `main actor-isolated ... in a synchronous nonisolated context` means "
    "annotate the ENCLOSING declaration `@MainActor` (the test method, the "
    "helper, the closure's owner) — not the callee. Every helper a `@MainActor` "
    "test calls needs the same annotation.\n"
    "4. `requires that 'X' conform to 'Hashable'` (or `Equatable`, `Sendable`) "
    "means add the conformance to X's DECLARATION, not rewrite the caller.\n"
    "5. `missing return in ... expected to return 'T'` means a `switch` or "
    "`if` arm lacks `return` — add it; do not restructure the function.\n"
    "6. A Metal vertex function declared with `[[stage_in]]` / "
    "`[[attribute(n)]]` needs an `MTLVertexDescriptor` set on the pipeline "
    "descriptor, or `makeRenderPipelineState` throws at runtime.\n\n"
    "A diagnostic names the line to change. Change that line. Do not touch "
    "files or lines no diagnostic names.\n\n"
)


# ── 2. Fix hints ────────────────────────────────────────────────────────────

# (regex on the message, the hint). First match wins.
_FIX_HINTS: "list[tuple[re.Pattern, str]]" = [
    (re.compile(r"static member '(\w+)' cannot be used on instance"),
     "qualify the reference on this line as `Self.{0}` — do not remove "
     "qualifiers anywhere else"),
    (re.compile(r"call can throw but is not marked with 'try'"),
     "write `try` in front of that call (`try #require(...)`, `try f()`); the "
     "enclosing function must be `throws`, or the call sits in a do/catch."),
    (re.compile(r"errors thrown from here are not handled"),
     "declare the enclosing function `throws` (for a test: "
     "`@Test func name() throws {{`)"),
    (re.compile(r"main actor-isolated .* (?:in a synchronous nonisolated "
                r"context|from a nonisolated)"),
     "if the cited line is inside a closure handed to NotificationCenter, a "
     "Combine sink, DispatchQueue or Timer, wrap that closure's body in "
     "`Task { @MainActor in ... }` (or `MainActor.assumeIsolated { }` when it "
     "is known to run on main); otherwise annotate the ENCLOSING declaration "
     "`@MainActor` (a test method becomes `@MainActor func test…() async "
     "throws`) — never the callee (DEV-784)"),
    (re.compile(r"call to main actor-isolated"),
     "annotate the ENCLOSING declaration `@MainActor` (a test: "
     "`@MainActor func name() async throws {{`) and `await` the call — never "
     "mark the callee `nonisolated`"),
    (re.compile(r"'nil' is not compatible with expected argument type '([^']+)'"),
     "the parameter is not optional — pass a `{0}` value (for an OptionSet "
     "such as `MTLResourceOptions`, `[]`)"),
    (re.compile(r"invalid redeclaration of '(\w+)'"),
     "delete this duplicate declaration of `{0}` and use the one that "
     "already exists — never rename one copy to dodge the clash"),
    (re.compile(r"requires that '(\w+)' conform to '(\w+)'"),
     "add `: {1}` to the declaration of `{0}`"),
    (re.compile(r"missing return in .* expected to return"),
     "add `return` to the arm that lacks it"),
    (re.compile(r"'(\w+)' is inaccessible due to '(\w+)' protection level"),
     "widen `{0}`'s access at its declaration (e.g. `private(set) var` or "
     "`internal`)"),
    (re.compile(r"incorrect argument label in call \(have '([^']+)', "
                r"expected '([^']+)'\)"),
     "use the labels `{1}`"),
    (re.compile(r"cannot find '(\w+)' in scope"),
     "`{0}` is not declared in any file you were given — it lives in a file "
     "outside your set (use it as declared there; do NOT invent a "
     "declaration for it) or the name is misspelled; if the build also "
     "reports a failed emit-module, this is a consequence of that, not the "
     "cause"),
    (re.compile(r"(?:value of )?type '([\w.]+)' has no member '(\w+)'"),
     "`{0}` has no `{1}` — that name is invented; use a member `{0}` "
     "actually declares (for a notification, the `Notification.Name` "
     "constant, e.g. `.AVAudioEngineConfigurationChange`)"),
    # Run 50 (spec_aba12b2b) retry 0: `'AVAudioSession' is unavailable in
    # macOS` twice — an iOS/visionOS-only API used outside its #if branch.
    (re.compile(r"'([\w.]+)' is unavailable in (\w+)"),
     "`{0}` does not exist on {1} — wrap the use in "
     "`#if os(iOS) || os(visionOS)` (with a macOS branch that compiles), do "
     "not remove the {1} build"),
]


def fix_hint(message: str) -> str | None:
    """The fix a diagnostic *message* asks for, or None when unknown."""
    for pattern, hint in _FIX_HINTS:
        m = pattern.search(message)
        if m:
            try:
                return hint.format(*m.groups())
            except (IndexError, KeyError):
                return hint
    return None


# DEV-784: rendered only when the spec's test_strategy declares the target's
# default isolation, so every other prompt stays byte-identical.
def render_default_isolation_rule(isolation: "str | None") -> str:
    if not isolation or isolation.strip().lower() != "mainactor":
        return ""
    return (
        "## This target is default-isolated to the main actor\n\n"
        "The project builds with `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`: "
        "EVERY unannotated declaration in the app target is `@MainActor`, with "
        "no annotation in the text to show it. Consequences:\n"
        "- A closure handed to `NotificationCenter.addObserver(forName:…using:)`, "
        "a Combine `.sink { }`, `DispatchQueue….async { }` or a `Timer` runs as a "
        "nonisolated synchronous context. Calling an instance method or touching "
        "a property inside it is the error `call to main actor-isolated instance "
        "method … in a synchronous nonisolated context`. Hop first: "
        "`Task { @MainActor in … }`, or `MainActor.assumeIsolated { }` when the "
        "closure is known to run on main.\n"
        "- Do not add `@MainActor` to types (redundant) and do not add "
        "`nonisolated` to anything that touches their state.\n"
        "- The TEST target is NOT default-isolated: a test that constructs or "
        "calls an app-target type is `@MainActor func test…() async throws`.\n\n"
    )
