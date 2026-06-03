"""Unit tests for manifest-mode infra (#4): parser, mode select, summary, builders."""
from qwen_autonomous import executor
from qwen_autonomous.executor import ManifestEntry, ParseError


# ── parse_manifest_response ──────────────────────────────────────────────────

def test_parse_manifest_basic_dependency_order():
    raw = """<<<MANIFEST>>>
shared/handshake.ts | contract types | CapabilityProfile, Verdict
server/resolver.ts | three-phase resolve | resolve, deriveTier
client/main.ts | entry point |
<<<END_MANIFEST>>>"""
    res = executor.parse_manifest_response(raw)
    assert not isinstance(res, ParseError)
    assert [e.path for e in res.entries] == [
        "shared/handshake.ts", "server/resolver.ts", "client/main.ts",
    ]
    assert res.entries[0].purpose == "contract types"
    assert res.entries[0].exports == "CapabilityProfile, Verdict"
    assert res.entries[2].exports == ""  # empty exports field tolerated


def test_parse_manifest_strips_list_markers_backticks_and_dupes():
    raw = """<<<MANIFEST>>>
1. `pkg/a.ts` | a | A
- pkg/b.ts | b | B
2) /pkg/a.ts | a again (dupe) | A2
# a comment line
<<<END_MANIFEST>>>"""
    res = executor.parse_manifest_response(raw)
    assert not isinstance(res, ParseError)
    # leading "1.", "- ", "2)" stripped; backticks stripped; leading / stripped;
    # duplicate path collapsed (first wins).
    assert [e.path for e in res.entries] == ["pkg/a.ts", "pkg/b.ts"]


def test_parse_manifest_missing_block_and_empty():
    assert isinstance(executor.parse_manifest_response("no markers here"), ParseError)
    assert isinstance(
        executor.parse_manifest_response("<<<MANIFEST>>>\n\n<<<END_MANIFEST>>>"),
        ParseError,
    )


def test_parse_manifest_tolerates_bracket_drift():
    # Qwen emits <MANIFEST>/<<MANIFEST>> non-deterministically; regex accepts 1-3.
    raw = "<MANIFEST>\nx/y.ts | thing | Z\n<END_MANIFEST>"
    res = executor.parse_manifest_response(raw)
    assert not isinstance(res, ParseError)
    assert res.entries[0].path == "x/y.ts"


# ── use_manifest_mode ────────────────────────────────────────────────────────

def _design_with_files(n):
    return "## File Structure\n" + "\n".join(f"dir/f{i}.ts" for i in range(n))


def test_use_manifest_mode_auto_threshold(monkeypatch):
    monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "auto")
    monkeypatch.setattr(executor, "MANIFEST_FILE_THRESHOLD", 8)
    assert executor.use_manifest_mode(_design_with_files(3)) is False
    assert executor.use_manifest_mode(_design_with_files(20)) is True


def test_use_manifest_mode_forced(monkeypatch):
    monkeypatch.setattr(executor, "MANIFEST_FILE_THRESHOLD", 8)
    monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "manifest")
    assert executor.use_manifest_mode(_design_with_files(1)) is True
    monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "single")
    assert executor.use_manifest_mode(_design_with_files(50)) is False


# ── summarize_written_files ──────────────────────────────────────────────────

def test_summarize_extracts_interface_lines():
    files = [
        ("a.ts", "import x from 'y';\nexport interface Foo { a: number }\nconst hidden = 1;\nfunction helper() {}\n"),
    ]
    out = executor.summarize_written_files(files)
    assert "### a.ts" in out
    assert "export interface Foo" in out
    assert "import x from 'y'" in out
    assert "function helper" in out
    assert "const hidden = 1" not in out  # non-interface body line excluded


def test_summarize_empty_is_explicit():
    assert "none yet" in executor.summarize_written_files([])


def test_summarize_is_bounded():
    big = [("f.ts", "\n".join(f"export const v{i} = {i};" for i in range(1000)))]
    out = executor.summarize_written_files(big, max_sig_lines=5)
    # only the first few signature lines retained
    assert out.count("export const") <= 5


# ── builders ─────────────────────────────────────────────────────────────────

def test_build_manifest_message_shape():
    msgs = executor.build_manifest_message("SPEC", "DESIGN", clarifications=["use TS"])
    assert msgs[0]["role"] == "system" and "MANIFEST" in msgs[0]["content"]
    u = msgs[1]["content"]
    assert "SPEC" in u and "DESIGN" in u and "use TS" in u


def test_build_per_file_message_targets_one_file():
    manifest = [ManifestEntry("shared/t.ts", "types", "T"),
                ManifestEntry("client/m.ts", "entry", "")]
    msgs = executor.build_per_file_message(
        "SPEC", "DESIGN", manifest, manifest[1], "### shared/t.ts\nexport type T = number",
        rejection_notes="fix the import",
    )
    u = msgs[1]["content"]
    assert "client/m.ts" in u
    assert "<<<FILE: client/m.ts>>>" in u
    assert "shared/t.ts" in u            # manifest + written summary present
    assert "fix the import" in u         # rejection feedback threaded
