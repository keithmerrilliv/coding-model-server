"""Compiler and test-runner output, read in one place — DEV-838.

Seven parsers across four modules each read the same build and test logs, and
each had its own copy of "what an error line looks like". They live here now,
moved verbatim; the old names stay importable from ``outcome``,
``gate_output`` and the daemon as aliases. :func:`read` composes them into
one :class:`Report` and adds no judgement of its own.

This module is a leaf: it imports only the standard library, so the kernel,
``citations``, the language packs and the daemon can all depend on it
without a cycle.

Map: what each piece matches; its callers; the tests that pin it.

1. Located compiler diagnostics and path normalization
 - ANSI_SGR_RE: SGR colour escapes (DEV-755); every stripper here, the daemon's
   feedback; test_outcome_diagnostics. This is the one copy.
 - SIG_PATH_RE, SIG_ERROR_RE, ATTRIBUTED_ERROR_RE / attributed_diagnostics,
   diagnostic_messages: location-stripped message per `path:l:c: error:` line;
   outcome._record, retry_policy, daemon repair and widening, the completeness
   note; test_outcome_diagnostics, test_diagnostic_classes,
   test_targeted_retry_widening, test_synthesis_repair_rollback.
 - DIAG_* / classify_diagnostic, diagnostic_classes, diagnostic_symbols: the
   DEV-529 closed class set; outcome._record, persistent_diagnostics;
   test_diagnostic_classes.
 - CITED_FILE_RE / cited_files, repo_relative: files the errors name, cut at a
   known root segment; outcome._record, coarse_key, test_runner;
   test_diagnostic_classes, test_invariant_failure_detection.
 - short_diagnostic_path: cut after `/worktrees/<dispatch>/`; the warning
   parser; test_build_warnings_as_signal.
 - KEY_PATH_RE, DIAG_MESSAGE_RE / diagnostic_identity: a failure detail's path
   and first compiler message; outcome.coarse_key;
   test_invariant_failure_detection.
 - LOCATED_RE, MACRO_LOCATED_RE / located_diagnostics, map_to_artifact: located
   errors incl. macro expansions (DEV-791); the daemon's build feedback and
   synthesis repair, via ``citations``; test_swift_rules,
   test_swift_prechecks_dev791.

2. The build verdict
 - BUILD_FAILURE_RES, BUILD_COMPLETE_RES, LINE_ATTRIBUTED_RE,
   COMPILE_STAGE_ERROR_RE / detect_build_failure: "never built" (DEV-429,
   DEV-548); pre-gate check, synthesis repair; test_pregate_build_check,
   test_build_failure_vs_crashed_run, test_build_check_reporting.
 - BARE_ERROR_RE / unattributed_errors: `error:` with no location (DEV-435);
   targeted-retry widening, the completeness note;
   test_unattributed_diagnostics.
 - TEST_PROCESS_CRASH_RE / detect_test_process_crash: the harness died on a
   signal (DEV-548); pre-gate check, gate build line;
   test_build_failure_vs_crashed_run.
 - PYTEST_SUMMARY_RE, NODE_TEST_SUMMARY_RE, VITEST_SUMMARY_RE /
   validate_test_output_structure: a summary line exists; the run guard;
   test_node_test_runner, test_node_modules_provisioning.
 - SWIFT_SUMMARY_RE / observed_a_test_run: positive evidence tests ran
   (DEV-477, DEV-787); pre-gate check, gate build line;
   test_build_check_reporting.

3. Test summaries and rosters
 - XCTEST_CASE_RE, SWIFT_TESTING_CASE_RE, PYTEST_FAILED_RE,
   PYTEST_PASSED_SUMMARY_RE, VERDICT_BANNERS, SWIFT_TESTING_RUN_VERDICT_RE,
   XCTEST_ALL_SUITE_RE, COMPILE_ERROR_LINE_RE / summarize_test_output: the
   gate's verdict, roster and errors (DEV-731, DEV-774); render_for_gate;
   test_gate_output.
 - XCODEBUILD_CASE_RE, SWIFT_TESTING_RUN_COUNT_RE, SWIFT_TESTING_FAILED_RE,
   XCTEST_EXECUTED_RE / swift_pass_rate, pass_rate: pass fraction (DEV-406,
   DEV-792); synthesis repair; test_swift_pass_rate_dev792,
   test_synthesis_repair.

4. Warnings
 - BUILD_WARNING_RE, WARNING_DIAG_ID_RE, BLOCKING_WARNING_RES / BuildWarning,
   parse_build_warnings, blocking_build_warnings (DEV-547); pre-gate check,
   synthesis repair; test_build_warnings_as_signal.

Look-alikes kept apart, because they differ on real input (measured over the
351 archived runner outputs):

* ``SIG_ERROR_RE`` finds ``error: `` anywhere in a line already known to be
  attributed; ``BARE_ERROR_RE`` wants it at the start of a line, which is what
  makes it "unattributed".
* ``SWIFT_TESTING_RUN_VERDICT_RE`` (the gate's) is line-anchored and captures
  passed/failed; ``SWIFT_TESTING_RUN_COUNT_RE`` (the pass rate's) is unanchored
  and captures the test count.
* ``ATTRIBUTED_ERROR_RE`` allows a space in the path; ``CITED_FILE_RE`` does
  not (see cited_files).
* ``repo_relative`` and ``short_diagnostic_path`` normalise differently: the
  first cuts at a known root segment and otherwise keeps the basename, the
  second cuts after ``/worktrees/<dispatch>/`` and otherwise keeps the whole
  path. They disagree on every path the VM's xcodebuild prints
  (``/Users/admin/work/ElectricSheepTests/X.swift``: ``X.swift`` against the
  full path).
* The roster counts and ``pass_rate`` read different lines. ``pass_rate`` reads
  node:test's TAP footer and XCTest's "Executed N tests", which the roster has
  no shape for; and it tries the Swift shapes before pytest's, so a pytest log
  whose test ids quote Swift output reads as that Swift run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, NamedTuple


# ── 1. Located compiler diagnostics and path normalization ──────────────────
# The failure_classified stream is the one place a failure's identity lives
# (DEV-631), and these parsers produce that identity. Absolute worktree paths
# differ per dispatch (…/worktrees/spec_x-7f8a8795/…) and line numbers move as
# the file is rewritten; neither changes what the defect IS, so both are
# stripped.
# DEV-755: swiftc colourises diagnostics, and the escape lands BETWEEN the
# location and the keyword — `Game.swift:109:38: \x1b[1;31merror: \x1b[1;39mvalue
# of...`. ATTRIBUTED_ERROR_RE needs a literal ": error: " and never matches, so a
# genuinely failing build reports ZERO attributed diagnostics. Run 45's repair
# gate then compared 0 -> 0 and read its own blindness as "did not improve".
# Escapes also pollute the captured message, so the same defect seen twice can
# compare unequal — which corrupts the failure IDENTITY this module exists to
# produce (DEV-631). xcodebuild output is not coloured, which is why run 44 saw
# a real 6 -> 36 and this stayed hidden until a swift_test run hit it.
ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

SIG_PATH_RE = re.compile(r"(/\S+?/)?([\w.+-]+\.\w+):\d+:\d+:")
SIG_ERROR_RE = re.compile(r"error: (.+)")
ATTRIBUTED_ERROR_RE = re.compile(r"^\s*\S.*?:\d+:\d+: error: ", re.MULTILINE)


def attributed_diagnostics(notes: str) -> list:
    """Location-stripped message of every attributed diagnostic, in order.

    One entry per diagnostic *occurrence*. Callers asking "which defects are
    here?" build a set from this; callers asking "did the build get worse?"
    count it. Those are different questions, and the gap between them is
    wide: run 8's repair output carries 27 diagnostics drawn from 6 distinct
    messages, so deduplicating first discards most of the magnitude. A set
    comparison can therefore score a regression as an improvement whenever
    the new errors repeat one message — which is exactly what a dropped
    import does (DEV-541).

    Only diagnostics that name a file:line say anything about the code. Bare
    driver lines — `error: fatalError`, `error: emit-module command failed…`
    — appear in essentially every failed build regardless of cause;
    counting them made every pair of consecutive failures look like the
    same unfixable defect (spec_cc7dd609).
    """
    if not notes:
        return []
    notes = ANSI_SGR_RE.sub("", notes)
    msgs = []
    for line in notes.splitlines():
        if not ATTRIBUTED_ERROR_RE.search(line):
            continue
        match = SIG_ERROR_RE.search(line)
        if not match:
            continue
        msg = SIG_PATH_RE.sub(r"\2:", match.group(1).strip())
        if msg:
            msgs.append(msg)
    return msgs


def diagnostic_messages(notes: str) -> set:
    """The distinct error messages in a failure report, location-stripped."""
    return set(attributed_diagnostics(notes))


# ── DEV-529: a closed set of diagnostic classes ──────────────────────────────
#
# Runs 1–7 produced a repeating failure taxonomy that lived only in prose:
# missing conformance (run 6's Mushroom, never Equatable), mutability (`let`
# where `var` is needed, `mutating` on a class method), undeclared type,
# file placement (a test one character off its directory, a module the
# sandbox cannot see), cross-file drift (signatures diverging between
# manifest-generated files). Each is classified at capture time onto the
# failure_classified row beside the raw text, so "failures by class, by
# agent, by retry" is a query and not a re-read of the logs. The class is a
# lossy index, never a replacement: unrecognised output is `other`, and
# `other` staying large is itself the signal that the set needs a member.
DIAG_MISSING_CONFORMANCE = "missing_conformance"
DIAG_MUTABILITY = "mutability"
DIAG_UNDECLARED_TYPE = "undeclared_type"
DIAG_FILE_PLACEMENT = "file_placement"
DIAG_CROSS_FILE_DRIFT = "cross_file_drift"
DIAG_OTHER = "other"
DIAGNOSTIC_CLASSES = (DIAG_MISSING_CONFORMANCE, DIAG_MUTABILITY,
                      DIAG_UNDECLARED_TYPE, DIAG_FILE_PLACEMENT,
                      DIAG_CROSS_FILE_DRIFT, DIAG_OTHER)
# Order matters: the first match wins, and the specific classes come before
# the broad ones (a conformance error also mentions a type name).
DIAG_CLASS_RES = (
    (DIAG_MISSING_CONFORMANCE, re.compile(
        r"does not conform to(?: protocol)?\b|requires that .+ conform to|"
        r"must conform to|no protocol conformance|protocol requirements? .+ not|"
        r"missing conformance|unsupported operand type|"
        r"not supported between instances|object is not (?:iterable|subscriptable|hashable)",
        re.I)),
    (DIAG_MUTABILITY, re.compile(
        r"'mutating'|cannot assign to (?:property|value|immutable)|"
        r"is a 'let' constant|cannot use mutating (?:member|getter|setter)|"
        r"immutable value|cannot pass immutable value|"
        r"marked with 'let'|object does not support item assignment|"
        r"can't set attribute|cannot assign to field",
        re.I)),
    (DIAG_FILE_PLACEMENT, re.compile(
        r"no such module|No module named|ModuleNotFoundError|"
        r"cannot find module|module '.+' has no attribute|"
        r"file not found|no such file or directory|"
        r"is not part of (?:the|any) (?:target|module)|"
        r"could not find module|found no tests|collected 0 items",
        re.I)),
    (DIAG_CROSS_FILE_DRIFT, re.compile(
        r"has no member|incorrect argument label|extra argument|"
        r"missing argument(?:s)? for parameter|cannot convert value of type|"
        r"argument type .+ does not|cannot import name|"
        r"unexpected keyword argument|"
        r"takes \d+ positional arguments? but|"
        r"missing \d+ required positional argument|"
        r"is not callable|no exact matches in call|"
        r"has no attribute|ambiguous use of|"
        r"argument passed to call that takes no arguments|"
        r"initializer .+ requires|cannot call value of non-function type",
        re.I)),
    (DIAG_UNDECLARED_TYPE, re.compile(
        r"cannot find (?:type )?'[^']+' in scope|use of undeclared|"
        r"NameError|name '[^']+' is not defined|undefined (?:symbol|reference)|"
        r"unresolved identifier|cannot find '[^']+'|is not a member type|"
        r"no type named|unknown type name|undeclared identifier",
        re.I)),
)
SYMBOL_RE = re.compile(r"'([A-Za-z_][\w.]*)'")
# Names that recur in unrelated diagnostics and would make any two attempts
# look like the same defect.
SYMBOL_NOISE = frozenset({
    "Int", "Int64", "UInt", "String", "Bool", "Double", "Float", "Any", "Void",
    "Self", "self", "None", "str", "int", "float", "bool", "list", "dict",
    "tuple", "set", "object", "Optional", "Array", "Dictionary", "Error",
    "Equatable", "Hashable", "Codable", "Sendable", "Comparable", "let", "var",
})


def classify_diagnostic(message: str) -> str:
    """The class of one location-stripped diagnostic message (DEV-529).
    Never raises; unrecognised text is ``other``."""
    text = message or ""
    for cls, pattern in DIAG_CLASS_RES:
        try:
            if pattern.search(text):
                return cls
        except Exception:  # a pathological message must not fail a run
            return DIAG_OTHER
    return DIAG_OTHER


def diagnostic_classes(messages: Iterable[str]) -> list:
    """Sorted distinct classes over *messages*."""
    return sorted({classify_diagnostic(m) for m in messages})


def diagnostic_symbols(messages: Iterable[str]) -> set:
    """The quoted identifiers the diagnostics name, minus the noise — the
    cheapest stable proxy for "the same defect wearing a different
    symptom" (DEV-509: `cannot find 'SeededRNG'` one attempt, `'SeededRNG'
    has no member 'next'` the next)."""
    out: set = set()
    for m in messages:
        for sym in SYMBOL_RE.findall(m or ""):
            leaf = sym.split(".")[-1]
            if leaf and leaf not in SYMBOL_NOISE and len(leaf) > 1:
                out.add(leaf)
    return out


# Unlike ATTRIBUTED_ERROR_RE, the path here may not contain a space, so an
# error in `LLab Shared/…` or at `macro expansion #require:1:1:` is attributed
# but no file is cited. cited_files also reads its text as given, without the
# ANSI strip attributed_diagnostics does: on raw coloured swiftc output it
# cites nothing. The failure stream passes it the daemon's build-failure note,
# which is built from stripped output.
CITED_FILE_RE = re.compile(r"^\s*(\S+?):\d+:\d+: error: ", re.MULTILINE)


def cited_files(notes: str) -> list:
    """Distinct files the attributed diagnostics name, in order, as the
    repository-relative path when one can be read off the absolute worktree
    path and the basename otherwise."""
    seen: list = []
    for m in CITED_FILE_RE.finditer(notes or ""):
        rel = repo_relative(m.group(1))
        if rel not in seen:
            seen.append(rel)
    return seen


REPO_ROOT_SEGMENTS = ("Sources", "Tests", "src", "tests", "lib", "app", "Packages")


def repo_relative(path: str) -> str:
    """The repository-relative form of a path a diagnostic named.

    The Mac runner materialises a fresh worktree per dispatch
    (`…/worktrees/<spec>-<hash>/Sources/…`, hash differs every time), so an
    absolute path is different on every attempt while naming the same file.
    Cut at the first recognised repository root segment; a path with none
    (a bare basename, or a layout we do not know) keeps its basename only.
    A path that is already relative and starts at such a segment is returned
    unchanged (DEV-672).
    """
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part in REPO_ROOT_SEGMENTS:
            return "/".join(parts[i:])
    return parts[-1]


# The warning parser's normaliser, and not the same function as repo_relative:
# this one keys on the runner's `/worktrees/` directory and keeps whatever
# follows the dispatch directory, so an Xcode project's `CentipedeApp/Views/…`
# survives here where repo_relative would keep only the basename. A path
# outside a worktree is returned whole rather than cut to its basename.
def short_diagnostic_path(path: str) -> str:
    """Drop the per-dispatch worktree prefix, keeping the repo-relative tail.

    Runner paths look like
    `/Users/youruser/…/worktrees/spec_9ff962b9-09f0ad65/Sources/CentipedeCore/World.swift`
    and the prefix changes on every dispatch, so it is noise in an artifact and
    breaks any comparison against `protected_paths`.
    """
    norm = (path or "").replace("\\", "/")
    marker = "/worktrees/"
    idx = norm.find(marker)
    if idx == -1:
        return norm
    tail = norm[idx + len(marker):]
    # …/worktrees/<dispatch-dir>/<repo-relative path>
    parts = tail.split("/", 1)
    return parts[1] if len(parts) == 2 else norm


# DEV-631: the path a failure is about, for outcome.coarse_key. The block
# number and similarity score an unappliable-edit detail carries are exactly
# the volatile particulars an identity must ignore; the file is what is left.
KEY_PATH_RE = re.compile(r"((?:[\w.+-]+/)+[\w.+-]+\.[A-Za-z0-9]+)")


# DEV-783: for a build failure the compiler's message IS the identity. Run 50
# keyed retry 0 (an invented API member) and retry 1 (a placeholder line the
# pipeline itself inserted) both as `build_failure||AudioManager.swift`, and
# the guard ended the rotation at 2 of 5 on two unrelated defects. Every
# attempt on a modify-spec touches the same files, so class+file collides by
# construction; class+file+message is what run 29 (the same error reproduced
# from the design) actually looked like. Other classes keep the class+file
# key: an unappliable-edit detail carries block numbers and scores that are
# exactly the volatile particulars an identity must ignore.
DIAG_MESSAGE_RE = re.compile(r":\d+:\d+:\s*(?:error|warning):\s*(.+)$")


def diagnostic_identity(detail: str) -> str:
    """The first compiler message in *detail*, normalised, or ""."""
    first = (detail or "").strip().splitlines()[0] if (detail or "").strip() else ""
    m = DIAG_MESSAGE_RE.search(first)
    msg = m.group(1) if m else ""
    return " ".join(msg.lower().split())[:160]


# DEV-764/767: the `path:line` citations a build reported, for the repair
# round's cite-or-refuse filter and the per-diagnostic fix hints in
# ``citations``. Unlike attributed_diagnostics these keep the location.
LOCATED_RE = re.compile(
    r"^\s*(?P<path>\S.*?\.\w+):(?P<line>\d+):(?P<col>\d+): error: (?P<msg>.+?)\s*$",
    re.MULTILINE,
)
# DEV-791: Swift 6 reports an error inside a macro expansion (`#require`,
# `#expect`) at `macro expansion #name:1:1:` with NO path, and puts the real
# location on the next line as a note. Run 52's retry 3 (`#require` without
# `try`) produced nothing the pattern above could locate, so the retry was
# headlined with the bare "SwiftCompile ... failed" line.
MACRO_LOCATED_RE = re.compile(
    r"^\s*macro expansion #\w+:\d+:\d+: error: (?P<msg>.+?)\s*\n"
    r"\s*`- (?P<path>\S.*?\.\w+):(?P<line>\d+):(?P<col>\d+): note: expanded code originates here",
    re.MULTILINE,
)


@dataclass(frozen=True)
class LocatedDiagnostic:
    path: str        # as the compiler printed it (often absolute)
    line: int
    message: str
    artifact: str | None = None   # the artifact relpath it maps onto, if any

    def located(self) -> str:
        return f"{self.artifact or self.path}:{self.line}"


def map_to_artifact(diag_path: str, artifact_paths: "list[str]") -> str | None:
    """The artifact relpath *diag_path* names, by path-boundary suffix.

    The compiler prints the worktree's absolute path
    (``/Users/km4/.../worktrees/spec_x-abc/Sources/A/B.swift``); the artifact
    is ``Sources/A/B.swift``. Longest suffix match wins so ``Tests/X.swift``
    is not confused with ``Sources/Tests/X.swift``.
    """
    diag_path = diag_path.replace("\\", "/")
    best: str | None = None
    for rel in artifact_paths:
        r = rel.replace("\\", "/").lstrip("./")
        if diag_path == r or diag_path.endswith("/" + r):
            if best is None or len(r) > len(best):
                best = rel
    return best


def located_diagnostics(output: str,
                        artifact_paths: "list[str] | None" = None
                        ) -> list[LocatedDiagnostic]:
    """Every ``path:line:col: error: msg`` in *output*, ANSI-stripped, in
    order, deduplicated on (path, line, message)."""
    if not output:
        return []
    text = ANSI_SGR_RE.sub("", output)
    seen: set = set()
    out: list[LocatedDiagnostic] = []
    matches = sorted(
        list(LOCATED_RE.finditer(text)) + list(MACRO_LOCATED_RE.finditer(text)),
        key=lambda m: m.start())
    for m in matches:
        key = (m.group("path"), int(m.group("line")), m.group("msg"))
        if key in seen:
            continue
        seen.add(key)
        art = map_to_artifact(m.group("path"), artifact_paths or [])
        out.append(LocatedDiagnostic(path=m.group("path"),
                                     line=int(m.group("line")),
                                     message=m.group("msg"), artifact=art))
    return out


# ── 2. The build verdict: failed, completed, crashed, inconclusive ──────────

# DEV-429: signatures of a *build* failure, as opposed to a test failure. The
# distinction matters because a build failure needs no human judgement — the
# compiler already said what is wrong — so it must never open a code_review
# gate. Swift emits `path:line:col: error: message` for every diagnostic and
# prints nothing of the sort when the build succeeds and only assertions fail.
# Python's equivalent is a collection/import error, which likewise means the
# suite never ran.
BUILD_FAILURE_RES = {
    "swift_test": re.compile(r"^.*:\d+:\d+: error: |^error: ", re.MULTILINE),
    "xcodebuild_test": re.compile(
        r"^.*:\d+:\d+: error: |^error: |The following build commands failed",
        re.MULTILINE),
    "pytest": re.compile(
        r"^E\s+(?:ImportError|ModuleNotFoundError|SyntaxError|IndentationError|NameError)|"
        r"^ERROR collecting |^!+ Interrupted: \d+ errors? during collection",
        re.MULTILINE),
}
BUILD_FAILURE_RES["python"] = BUILD_FAILURE_RES["pytest"]


# DEV-435: an `error:` the compiler emitted with no file:line in front of it.
# `swift build` reports a failed emit-module job as a bare
# "error: emit-module command failed with exit code 1 (use -v to see
# invocation)" and swallows the underlying diagnostic. Nothing downstream can
# act on that: the retry's file selection keys off cited paths and finds none,
# so it attributes the failure entirely to whatever cascade errors DID carry a
# location — usually the test files that can no longer see the module.
# Line-anchored, unlike SIG_ERROR_RE: an `error:` after a location is
# attributed, and that is the whole distinction.
BARE_ERROR_RE = re.compile(r"^error: (.+)$", re.MULTILINE)


def unattributed_errors(output: str) -> list[str]:
    """`error:` lines carrying no file:line, oldest first, deduped."""
    if not output:
        return []
    seen, out = set(), []
    for match in BARE_ERROR_RE.finditer(output):
        msg = match.group(1).strip()
        if msg and msg not in seen:
            seen.add(msg)
            out.append(msg)
    return out


# ── Built, then died: not the same as never built (DEV-548) ─────────────────
#
# `BUILD_FAILURE_RES["swift_test"]` accepts a bare `^error: ` line, which is
# what catches the unattributed compile-stage failures DEV-435 documented
# (`error: emit-module command failed`, `error: fatalError`). It also matches
# anything the *test harness* prints, including long after the build finished.
#
# Run 9 of DEV-102 ended with `Build complete! (3.00s)`, 19 tests launched, and
# then `error: Process '…swiftpm-testing-helper…' exited with unexpected signal
# code 5`. That was classified as "the code does not compile" — told to the
# model in those words, while the only located evidence in the output was the
# warning DEV-547 parses. Ordering is the cheap discriminator: an unattributed
# error *after* a completed build is not the compiler rejecting the code.
BUILD_COMPLETE_RES = {
    "swift_test": re.compile(r"^Build complete!", re.MULTILINE),
    "xcodebuild_test": re.compile(
        r"^\*\* BUILD SUCCEEDED \*\*|^Build complete!", re.MULTILINE),
}

# A diagnostic that names a file:line:col is always the compiler talking.
LINE_ATTRIBUTED_RE = re.compile(r":\d+:\d+: (?:error|warning): ")

# Driver lines that name a *compile* stage stay build failures wherever they
# appear, so DEV-435's case is untouched. `fatalError` is the swift driver
# reporting a crashed sub-job during the build and is kept here deliberately:
# demoting it would change today's behaviour on every failed Swift build.
COMPILE_STAGE_ERROR_RE = re.compile(
    r"error: .*\b(?:emit-module|compile|link|build)\b.*command failed"
    r"|error: fatalError"
    r"|The following build commands failed")

TEST_PROCESS_CRASH_RE = re.compile(
    r"^error: Process '(?P<proc>[^']*)' exited with unexpected signal code "
    r"(?P<signal>\d+)", re.MULTILINE)


def detect_test_process_crash(output: str) -> str | None:
    """Short reason when the test binary died on a signal (DEV-548).

    This is a third outcome beside "failed to build" and "tests failed": the
    code compiled, the harness started, and the process was killed before it
    could report. Under `--parallel` a single trap takes every test down with
    it, which is why run 9 produced 19 started and 0 completed.

    A behavioural defect, not a build one — an out-of-range subscript, a
    force-unwrapped nil, a failed precondition.
    """
    if not output:
        return None
    match = TEST_PROCESS_CRASH_RE.search(output)
    if match is None:
        return None
    return (f"the test process exited on signal {match.group('signal')} "
            f"before any test reported")


def detect_build_failure(output: str, framework: str, passed: bool) -> str | None:
    """Return a short reason when *output* shows the code never built.

    Only ever consulted on a failing run: a green suite proves the build was
    fine, and some tests legitimately print the word "error" in their own
    output. Returning None means "this is a real test failure, or we cannot
    tell" — both of which keep the normal gate path.

    DEV-548: an unattributed `error:` printed after the build completed is not
    a build failure. It is some later process exiting non-zero, and calling it
    a compile failure makes the pipeline state something false to the model.
    """
    if passed or not output:
        return None
    pattern = BUILD_FAILURE_RES.get(framework.lower())
    if pattern is None:
        return None
    complete = BUILD_COMPLETE_RES.get(framework.lower())
    complete_match = complete.search(output) if complete else None
    completed_at = complete_match.start() if complete_match else None

    for match in pattern.finditer(output):
        line = output[match.start():].splitlines()[0].strip()
        if not line:
            continue
        # The compiler naming a file, or a driver naming a compile stage:
        # a build failure wherever it appears.
        if (LINE_ATTRIBUTED_RE.search(line)
                or COMPILE_STAGE_ERROR_RE.search(line)):
            return line[:200]
        # Bare `error:` after the build finished — a later process failing,
        # not the compiler. Keep looking; something earlier may be real.
        if completed_at is not None and match.start() > completed_at:
            continue
        return line[:200]
    return None


# Pytest summary line: e.g. "1 passed in 0.01s", "2 failed, 3 passed in 0.5s",
# "5 errors in 1.0s". We only care that *some* outcome count is reported.
PYTEST_SUMMARY_RE = re.compile(
    r"\b\d+\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b",
    re.IGNORECASE,
)
# Node's built-in test runner (node:test) TAP footer: lines like
# "# tests 2", "# pass 1", "# fail 1". Any one of these confirms a real run.
NODE_TEST_SUMMARY_RE = re.compile(r"^# (?:tests|pass|fail)\s+\d+", re.MULTILINE)
# Vitest summary block (DEV-104):
#     Test Files  1 passed (1)
#          Tests  3 passed (3)
# Deliberately anchored on the "Tests"/"Test Files" counter rather than the
# `Test Files` line alone: vitest prints "Test Files  no tests" when it
# collects nothing, which must NOT read as a successful run. Requiring a
# digit-led outcome means an empty collection fails the guard.
VITEST_SUMMARY_RE = re.compile(
    r"^\s*(?:Test Files|Tests)\s+\d+\s+(?:passed|failed|skipped|todo)",
    re.MULTILINE,
)


def validate_test_output_structure(test_output: str, framework: str) -> tuple[bool, str]:
    """Confirm the test output has the structural shape of a real test run.

    Catches the failure mode where a sandbox error or collection failure exits
    cleanly without any tests actually running, leaving the orchestrator with
    no evidence either way. Returns (ok, reason). On (False, reason) the
    caller should force tests_passed=False — the runner can't be trusted.

    Frameworks we don't recognize (Swift via mac-runner, custom) pass through.
    """
    fw = framework.lower()
    if fw in ("pytest", "python"):
        if not PYTEST_SUMMARY_RE.search(test_output):
            return False, "no pytest summary line ('N passed/failed/error') detected"
    elif fw == "jest":
        return True, ""
    elif fw == "vitest":
        if not VITEST_SUMMARY_RE.search(test_output):
            return False, "no vitest summary line ('Tests  N passed') detected"
    elif fw == "node_test":
        if not NODE_TEST_SUMMARY_RE.search(test_output):
            return False, "no node:test summary line ('# tests/# pass/# fail N') detected"
    return True, ""


# Swift emits no summary that validate_test_output_structure knows — it
# deliberately passes unrecognised frameworks through, which is right for the
# anti-hallucination guard (a false negative there would force a genuinely
# passing suite to FAIL) but useless for deciding what to tell a reviewer.
# swift-testing prints "✔/✘ Test run with N tests passed/failed after ..."
# and XCTest prints "Executed N tests, with M failures" plus "Test Suite '...'
# passed/failed". `xcodebuild test` under parallel testing prints none of
# those: only one "Test case 'Suite.name()' passed on 'My Mac ...'" line per
# case and the "** TEST SUCCEEDED **" / "** TEST FAILED **" verdict (DEV-787,
# run 51 was told "inconclusive" over a 65-case run). Matching any of them is
# evidence tests actually executed.
SWIFT_SUMMARY_RE = re.compile(
    r"Test run with \d+ test"
    r"|Executed \d+ test"
    r"|Test Suite '[^']*' (?:passed|failed)"
    r"|Test case '[^']+' (?:passed|failed)"
    r"|\*\*\s*TEST (?:SUCCEEDED|FAILED)\s*\*\*",
    re.MULTILINE)


def observed_a_test_run(output: str, framework: str) -> bool:
    """Is there positive evidence the runner executed tests? (DEV-477)

    Wording only — this never changes control flow, so a miss costs a vaguer
    gate prompt rather than a wrongly-failed suite. That asymmetry is
    deliberate: a pattern that is wrong here can only make the prompt vaguer,
    so it is the one safe place for one.
    """
    if not output or not output.strip():
        return False
    ok, _ = validate_test_output_structure(output, framework)
    if not ok:
        return False
    if framework.lower() in ("swift_test", "xcodebuild_test"):
        return bool(SWIFT_SUMMARY_RE.search(output))
    return True


# ── 3. Test summaries and rosters, per framework ────────────────────────────
# The release gate's reading (DEV-731): lead with the verdict and the roster,
# because head truncation of an xcodebuild log shows the dependency graph and
# no result at all.

# XCTest, as xcodebuild prints it. Note the lowercase "case" — the capitalised
# form does not appear, and grepping for "Test Case" finds nothing, which is a
# good way to conclude no tests ran when four of them did.
XCTEST_CASE_RE = re.compile(
    r"^Test case '([^']+)'\s+(passed|failed)\b", re.MULTILINE)
# swift-testing (@Test / @Suite), Xcode 16+.
SWIFT_TESTING_CASE_RE = re.compile(
    r'^[✔✘◇\s]*Test\s+(?:"([^"]+)"|(\S+?))\s+(passed|failed)\b', re.MULTILINE)
# pytest per-failure lines and its summary line. The summary pattern needs a
# "passed" count, unlike PYTEST_SUMMARY_RE (any outcome count), because it is
# read for the number of passes.
PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
PYTEST_PASSED_SUMMARY_RE = re.compile(
    r"^=+\s*(?:.*?\b(\d+) failed)?.*?\b(\d+) passed\b.*$", re.MULTILINE)
# The framework-level verdicts.
VERDICT_BANNERS = (
    (re.compile(r"\*\*\s*TEST SUCCEEDED\s*\*\*"), "TEST SUCCEEDED"),
    (re.compile(r"\*\*\s*TEST FAILED\s*\*\*"), "TEST FAILED"),
    (re.compile(r"\*\*\s*BUILD SUCCEEDED\s*\*\*"), "BUILD SUCCEEDED"),
    (re.compile(r"\*\*\s*BUILD FAILED\s*\*\*"), "BUILD FAILED"),
)
# DEV-774: `swift test` has no `** TEST SUCCEEDED **`. Swift Testing prints one
# run-level line per runner ("✔ Test run with 5 tests in 1 suite passed after
# 0.064 seconds."; a mixed target prints a second for the XCTest half), and
# XCTest under `swift test` prints "Test Suite 'All tests' passed/failed". Run
# 49's release gate read "No framework verdict found" beside correct counts.
# Not SWIFT_TESTING_RUN_COUNT_RE: this one is line-anchored and captures the
# run's verdict; that one is unanchored and captures its test count.
SWIFT_TESTING_RUN_VERDICT_RE = re.compile(
    r"^[✔✘◇\s]*Test run with \d+ tests? in \d+ suites? (passed|failed)\b",
    re.MULTILINE)
XCTEST_ALL_SUITE_RE = re.compile(
    r"^Test Suite 'All tests' (passed|failed) at", re.MULTILINE)
# Compiler/link errors, so a build failure names its cause rather than making
# the reviewer scroll for it.
COMPILE_ERROR_LINE_RE = re.compile(
    r"^(/[^\s:]+:\d+:\d+:\s+error:\s+.*|.*\berror:\s+no such module.*)$",
    re.MULTILINE)


@dataclass
class TestSummary:
    verdict: str | None = None
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Frameworks that report COUNTS rather than names (pytest's summary line).
    # Kept apart from the name lists so `executed` stays a real number instead
    # of counting a synthesised placeholder as one test.
    unnamed_passed: int = 0

    @property
    def n_passed(self) -> int:
        return len(self.passed) + self.unnamed_passed

    @property
    def executed(self) -> int:
        return self.n_passed + len(self.failed)


def summarize_test_output(output: str) -> TestSummary:
    """Pull the verdict, the test roster and any compile errors out of a log."""
    s = TestSummary()
    text = output or ""
    for pattern, label in VERDICT_BANNERS:
        if pattern.search(text):
            s.verdict = label
            break
    if s.verdict is None:
        runs = (SWIFT_TESTING_RUN_VERDICT_RE.findall(text)
                + XCTEST_ALL_SUITE_RE.findall(text))
        if runs:
            # One failed runner fails the run; only all-passed is a pass.
            s.verdict = "TEST RUN FAILED" if "failed" in runs else "TEST RUN PASSED"

    for name, status in XCTEST_CASE_RE.findall(text):
        (s.passed if status == "passed" else s.failed).append(name)
    if not s.executed:
        for quoted, bare, status in SWIFT_TESTING_CASE_RE.findall(text):
            name = quoted or bare
            (s.passed if status == "passed" else s.failed).append(name)
    if not s.executed:
        s.failed.extend(PYTEST_FAILED_RE.findall(text))
        if (m := PYTEST_PASSED_SUMMARY_RE.search(text)):
            # pytest names its failures but only counts its passes.
            s.unnamed_passed = int(m.group(2))

    seen: set[str] = set()
    for line in COMPILE_ERROR_LINE_RE.findall(text):
        line = line.strip()
        if line not in seen:
            seen.add(line)
            s.errors.append(line)
    return s


# DEV-792: the three Swift summary shapes. `xcodebuild test` prints one
# "Test case '…' passed|failed" line per case; `swift test` with Swift Testing
# prints "Test run with N tests … passed|failed" per target plus one
# "✘ Test name() failed after …" per failing test; XCTest under `swift test`
# prints "Executed N tests, with M failures". Run 52's synthesis was 30 of 31
# green and got no repair round because none of these was a "pass rate".
# XCODEBUILD_CASE_RE requires the " on '<destination>'" suffix, which
# XCTEST_CASE_RE does not.
XCODEBUILD_CASE_RE = re.compile(
    r"^Test case '[^']+' (passed|failed) on ", re.MULTILINE)
SWIFT_TESTING_RUN_COUNT_RE = re.compile(r"Test run with (\d+) tests? in ")
SWIFT_TESTING_FAILED_RE = re.compile(
    r"^\s*\S*\s*Test (?!run\b)\S+ failed after ", re.MULTILINE)
XCTEST_EXECUTED_RE = re.compile(r"Executed (\d+) tests?, with (\d+) failures?")


def swift_pass_rate(test_output: str) -> "float | None":
    """Pass fraction from a Swift runner's output, or None (DEV-792)."""
    text = ANSI_SGR_RE.sub("", test_output or "")
    cases = XCODEBUILD_CASE_RE.findall(text)
    if cases:
        return cases.count("passed") / len(cases)
    runs = [int(n) for n in SWIFT_TESTING_RUN_COUNT_RE.findall(text)]
    if runs and sum(runs) > 0:
        total = sum(runs)
        failed = len(SWIFT_TESTING_FAILED_RE.findall(text))
        return max(0.0, total - failed) / total
    executed = XCTEST_EXECUTED_RE.findall(text)
    if executed:
        total = sum(int(n) for n, _ in executed)
        failed = sum(int(m) for _, m in executed)
        if total > 0:
            return max(0.0, total - failed) / total
    return None


def pass_rate(test_output: str) -> "float | None":
    """Best-effort pass fraction from a runner summary; None if unparseable.

    None (not 0.0) on no-parse: an unreadable summary must not qualify for
    a repair round it can't be measured against.
    """
    tap_total = re.search(r"^# tests (\d+)$", test_output, re.MULTILINE)
    tap_pass = re.search(r"^# pass (\d+)$", test_output, re.MULTILINE)
    if tap_total and tap_pass and int(tap_total.group(1)) > 0:
        return int(tap_pass.group(1)) / int(tap_total.group(1))
    swift_rate = swift_pass_rate(test_output)
    if swift_rate is not None:
        return swift_rate
    passed = re.search(r"(\d+) passed", test_output)
    failed = re.search(r"(\d+) failed", test_output)
    if passed:
        n_pass = int(passed.group(1))
        n_fail = int(failed.group(1)) if failed else 0
        if n_pass + n_fail > 0:
            return n_pass / (n_pass + n_fail)
    return None


# ── 4. Warnings ──────────────────────────────────────────────────────────────
#
# Compiler warnings as signal (DEV-547). Run 9 of DEV-102 compiled, launched
# all 19 tests, and died on a runtime trap with zero tests completed. The
# defect was one inverted conditional that emptied `chains` on every step(),
# and the compiler had already named it, on the line, in output we captured
# and parsed:
#
#   World.swift:238:20: warning: value 'updateIdx' was defined but never used;
#                       consider replacing with boolean test [#no-usage]
#
# Errors drove control flow throughout the daemon; warnings were carried along
# as text and read by nobody. For model-written code that is the wrong trade.
# The warning classes a human reviewer learns to skim past are precisely the
# fingerprints of a model emitting confused control flow.
BUILD_WARNING_RE = re.compile(
    r"^\s*(\S.*?):(\d+):(\d+): warning: (.+)$", re.MULTILINE)

# The trailing `[#no-usage]` id modern Swift appends. Absent on older
# toolchains and on most other compilers, so it is recorded when present and
# never required for a match.
WARNING_DIAG_ID_RE = re.compile(r"\s*\[#([\w.-]+)\]\s*$")

# Deliberately narrow. A false positive costs a full implementer generation
# plus a runner dispatch, so this holds only classes where the compiler has
# *proved* that the code contradicts its apparent intent. Style warnings a
# human would rightly ignore — "never mutated; consider changing to 'let'",
# "was never used; consider replacing with '_'" on a loop index — are recorded
# and deliberately NOT blocked.
BLOCKING_WARNING_RES = (
    # `if let x = <expr>` where x is never read. Swift emits this only when the
    # binding is pointless, which means the condition is not testing what it
    # appears to test. Run 9's defect, verbatim.
    re.compile(r"was defined but never used", re.I),
    # A branch the model wrote and then made unreachable.
    re.compile(r"will never be executed", re.I),
    # A condition the compiler can fold to a constant.
    re.compile(r"comparison .*?always (?:true|false)", re.I),
    re.compile(r"condition is always (?:true|false)", re.I),
)


class BuildWarning(NamedTuple):
    """One `path:line:col: warning:` diagnostic lifted from a build."""
    path: str          # repo-relative where derivable, else as emitted
    line: int
    column: int
    diag_id: str       # "no-usage" etc., "" when the toolchain emits none
    message: str       # id stripped
    blocking: bool

    def located(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


def parse_build_warnings(output: str,
                         protected_paths=None) -> "list[BuildWarning]":
    """Every located warning in *output*, flagged for whether it should block.

    A warning on a protected path is never blocking: the pipeline cannot edit
    those files, so rejecting an attempt over one would loop forever (DEV-427
    drops them before dispatch, so the worktree holds `main`'s copy).

    The caret echo line the compiler prints under a diagnostic repeats the
    message verbatim but carries no `path:line:col`, so it never matches and
    no de-duplication is needed for it.
    """
    if not output:
        return []
    protected = {str(p).strip().lstrip("./")
                 for p in (protected_paths or []) if p}
    seen = set()
    found: list[BuildWarning] = []
    for match in BUILD_WARNING_RE.finditer(output):
        raw_path, line, column, message = match.groups()
        message = message.strip()
        diag_id = ""
        id_match = WARNING_DIAG_ID_RE.search(message)
        if id_match:
            diag_id = id_match.group(1)
            message = WARNING_DIAG_ID_RE.sub("", message).strip()
        path = short_diagnostic_path(raw_path)
        key = (path, line, column, message)
        if key in seen:
            continue
        seen.add(key)
        on_protected = path in protected or any(
            path.endswith("/" + p) for p in protected)
        blocking = not on_protected and any(
            r.search(message) for r in BLOCKING_WARNING_RES)
        found.append(BuildWarning(path, int(line), int(column),
                                  diag_id, message, blocking))
    return found


def blocking_build_warnings(output: str,
                            protected_paths=None) -> "list[BuildWarning]":
    """The subset of parse_build_warnings that should reject an attempt."""
    return [w for w in parse_build_warnings(output, protected_paths)
            if w.blocking]


# ── 5. One reading of one log ────────────────────────────────────────────────

VERDICTS = ("compile_failed", "crashed", "passed", "tests_failed", "inconclusive")


@dataclass(frozen=True)
class Report:
    """What one build or test log says, read once.

    ``verdict`` is the decision the daemon's pre-gate build check makes:
    passed; else compile_failed when detect_build_failure names a line; else
    crashed when the harness died on a signal; else tests_failed when there is
    positive evidence a test run happened; else inconclusive. The gate's
    build-check line asks "did tests run?" before "did it crash?"; no archived
    output is both, so the two orders agree on every run so far.
    """
    verdict: str
    # attributed_diagnostics: one location-stripped message per occurrence.
    diagnostics: tuple[str, ...]
    # summarize_test_output's failed names.
    roster: tuple[str, ...]
    # summarize_test_output's counts; None when it found no test result.
    passed: int | None
    failed: int | None
    total: int | None
    warnings: tuple[BuildWarning, ...]
    # The reason strings the verdict was derived from, as the daemon logs and
    # records them.
    build_failure: str | None
    crash: str | None
    # pass_rate(): the synthesis repair threshold's number. It reads the
    # summary lines, not the roster, so it can disagree with passed/total.
    pass_rate: float | None


def read(output: str, framework: str, *, passed: bool,
         protected_paths: "Iterable[str] | None" = ()) -> Report:
    """Read *output* from a *framework* run whose runner reported *passed*.

    *passed* is the runner's own result (after the structural guard), which
    the log alone cannot establish; every other field comes from the text.
    """
    text = output or ""
    build_failure = detect_build_failure(text, framework, passed)
    crash = detect_test_process_crash(text)
    if passed:
        verdict = "passed"
    elif build_failure:
        verdict = "compile_failed"
    elif crash:
        verdict = "crashed"
    elif observed_a_test_run(text, framework):
        verdict = "tests_failed"
    else:
        verdict = "inconclusive"
    summary = summarize_test_output(text)
    counted = summary.executed > 0
    return Report(
        verdict=verdict,
        diagnostics=tuple(attributed_diagnostics(text)),
        roster=tuple(summary.failed),
        passed=summary.n_passed if counted else None,
        failed=len(summary.failed) if counted else None,
        total=summary.executed if counted else None,
        warnings=tuple(parse_build_warnings(text, protected_paths)),
        build_failure=build_failure,
        crash=crash,
        pass_rate=pass_rate(text),
    )
