"""The per-file prompt keeps what is stable ahead of what varies.

llama-server's prompt cache reuses the longest common PREFIX of the previous
prompt and re-evaluates everything after the first byte that differs. A
manifest build sends one prompt per file to the same process, and those
prompts are mostly the same text: spec, design, manifest and the protected
reference files. With the target line ahead of the reference block, ten
consecutive calls of one spec each re-evaluated 113-116k of ~115k prompt
tokens, about 180 s apiece, while an identical prompt cost 4 tokens.

So the order is part of the contract: everything identical across the calls
of one attempt comes first, everything per-call comes after it, and the
system message does not change with the target.
"""
import os

from coding_model_autonomous.executor import (
    ManifestEntry,
    PER_FILE_EDIT_MODE_INSTRUCTIONS,
    build_per_file_message,
    summarize_written_files,
)

SPEC = "# Spec\n\n" + "The service keeps a ledger of entries. " * 40
DESIGN = "# Design\n\n" + "Each module owns one concern. " * 60
MANIFEST = [
    ManifestEntry("src/ledger.ts", "the ledger", "Ledger"),
    ManifestEntry("src/report.ts", "reporting over the ledger", "report"),
    ManifestEntry("src/cli.ts", "entry point", ""),
]
# Stands in for the protected scaffold: by far the largest block, as it is in
# the prompts this ordering exists for.
REFERENCE = [
    ("src/scaffold.ts", "\n".join(f"export const K{i} = {i};" for i in range(4000))),
    ("src/types.ts", "\n".join(f"export type T{i} = number;" for i in range(2000))),
]
LEDGER_SRC = "export class Ledger {\n  add(n: number): void {}\n}\n"
REPORT_SRC = "import { Ledger } from './ledger';\nexport function report(l: Ledger): string { return ''; }\n"


def _wire(messages):
    """The prompt as one string in the order the model server evaluates it."""
    return "\n".join(m["content"] for m in messages)


def _shared(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))


def _call(target, written, **kw):
    # omitted_reference=[] is the daemon's shape: the allocator has already
    # decided what is shown, so the render applies no knob of its own.
    return build_per_file_message(
        SPEC, DESIGN, MANIFEST, target, summarize_written_files(written),
        ["Use integer cents."], reference_files=REFERENCE,
        omitted_reference=[],
        approval_conditions="Keep Ledger immutable.", **kw)


def test_two_targets_of_one_attempt_share_the_prefix_through_the_reference_block():
    first = _wire(_call(MANIFEST[0], []))
    second = _wire(_call(MANIFEST[1], [("src/ledger.ts", LEDGER_SRC)]))
    shared = _shared(first, second)

    # The whole reference block, to its last byte, is inside the shared prefix.
    last_reference_line = "export type T1999 = number;\n````\n\n"
    assert shared >= first.index(last_reference_line) + len(last_reference_line)
    # And that is nearly all of the prompt: what is left to evaluate is the
    # per-call tail, not the stable body.
    assert shared / len(second) > 0.97
    # The calls really are for different files.
    assert "<<<FILE: src/ledger.ts>>>" in first[shared:]
    assert "<<<FILE: src/report.ts>>>" in second[shared:]


def test_nothing_that_names_the_target_precedes_the_stable_block():
    user = _call(MANIFEST[1], [("src/ledger.ts", LEDGER_SRC)],
                 rejection_notes="report() drops the last entry",
                 existing_content="export function report() {}\n")[1]["content"]
    end_of_stable = user.index("## Files already written")
    for stable in ("## Operator clarifications", "Reviewer conditions",
                   "## Specification", "## Architecture Design",
                   "## File Manifest", "may NOT change", "export const K3999"):
        assert user.index(stable) < end_of_stable, stable
    for varying in ("Write ONLY this one file now", "Expected exports",
                    "## Current content of src/report.ts",
                    "report() drops the last entry"):
        assert user.index(varying) > end_of_stable, varying


def test_the_omitted_listing_is_part_of_the_stable_block():
    user = build_per_file_message(
        SPEC, DESIGN, MANIFEST, MANIFEST[0], "none",
        reference_files=[], omitted_reference=["package-lock.json"],
    )[1]["content"]
    assert user.index("package-lock.json") < user.index("## Files already written")


def test_the_target_is_still_named_and_the_output_instruction_is_last():
    user = _call(MANIFEST[1], [("src/ledger.ts", LEDGER_SRC)],
                 rejection_notes="fix the import")[1]["content"]
    assert "Write ONLY this one file now:\n\n**src/report.ts**" in user
    assert user.endswith(
        "Output exactly one <<<FILE: src/report.ts>>> … <<<END_FILE>>> block.")

    edit = _call(MANIFEST[1], [], existing_content="x\n", edit_mode=True,
                 edit_errors="- edit block #1: SEARCH text not found")[1]["content"]
    assert edit.endswith("emit a <<<FILE: ...>>> whole-file block.")
    assert edit.index("previous edit blocks failed") > edit.index("## Current content")


def test_an_edited_target_does_not_change_the_system_message():
    """A manifest that mixes created and edited files must not lose the prefix
    at every switch: the edit rules ride the per-call tail, and the system
    message — the first thing evaluated — is the same for both."""
    created = _call(MANIFEST[0], [])
    edited = _call(MANIFEST[1], [("src/ledger.ts", LEDGER_SRC)],
                   existing_content="export function report() {}\n",
                   edit_mode=True)
    assert created[0] == edited[0]

    user = edited[1]["content"]
    assert PER_FILE_EDIT_MODE_INSTRUCTIONS in user
    # After the stable block, and ahead of the content the rules refer to.
    assert (user.index("export type T1999")
            < user.index(PER_FILE_EDIT_MODE_INSTRUCTIONS)
            < user.index("## Current content of src/report.ts"))
    wire = _wire(edited)
    assert _shared(_wire(created), wire) > wire.index("export type T1999")


def test_a_parse_retry_shares_everything_up_to_the_new_diagnostics():
    kw = dict(existing_content="export function report() {}\n", edit_mode=True)
    written = [("src/ledger.ts", LEDGER_SRC)]
    first = _wire(_call(MANIFEST[1], written, **kw))
    retry = _wire(_call(MANIFEST[1], written,
                        edit_errors="- edit block #1: SEARCH text not found", **kw))
    assert retry[_shared(first, retry):].lstrip().startswith(
        "## Your previous edit blocks failed to apply")


# ── the written-files summary is append-only ────────────────────────────────

def test_summary_of_one_more_file_extends_the_previous_summary():
    one = summarize_written_files([("src/ledger.ts", LEDGER_SRC)])
    two = summarize_written_files([("src/ledger.ts", LEDGER_SRC),
                                   ("src/report.ts", REPORT_SRC)])
    assert two.startswith(one)
    assert "### src/report.ts" in two[len(one):]


def test_summary_stays_append_only_across_the_size_cap():
    files = [(f"src/m{i}.ts", f"export function f{i}(): number {{ return {i}; }}\n")
             for i in range(12)]
    previous = ""
    for n in range(1, len(files) + 1):
        current = summarize_written_files(files[:n], max_total_chars=300)
        assert current.startswith(previous), n
        previous = current
    # The cap was actually reached, so the loop covered both sides of it.
    assert "further files omitted" in previous
    assert "### src/m11.ts" not in previous


def test_consecutive_calls_diverge_only_at_the_newest_summary_entry():
    second = _wire(_call(MANIFEST[1], [("src/ledger.ts", LEDGER_SRC)]))
    third = _wire(_call(MANIFEST[2], [("src/ledger.ts", LEDGER_SRC),
                                      ("src/report.ts", REPORT_SRC)]))
    shared = _shared(second, third)
    # The first file's whole summary entry is inside the shared prefix; the
    # prompts part ways where the second file's entry is appended.
    entry = "### src/ledger.ts\nexport class Ledger {"
    assert shared >= second.index(entry) + len(entry)
    assert third[shared:].lstrip().startswith("### src/report.ts")
