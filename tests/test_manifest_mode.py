"""Unit tests for manifest-mode infra (#4): parser, mode select, summary, builders."""
import pytest

from coding_model_autonomous import executor
from coding_model_autonomous.executor import ManifestEntry, ParseError


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
    # Coding Model emits <MANIFEST>/<<MANIFEST>> non-deterministically; regex accepts 1-3.
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


# ── manifest detection for designs the file-path regex misses (DEV-27) ────────

_RAILS_DESIGN = """## File Structure
app/models/user.rb
app/models/order.rb
app/controllers/users_controller.rb
app/controllers/orders_controller.rb
app/views/users/show.erb
app/services/billing.rb
config/routes.rb
db/schema.rb
Gemfile
Dockerfile
"""

_TREE_DESIGN = """## File Structure
myapp/
├── src/
│   ├── components/
│   │   ├── Header
│   │   ├── Footer
│   ├── hooks/
│   ├── utils/
│   ├── pages/
│   ├── api/
│   ├── store/
│   └── types/
├── tests/
└── public/
"""

_PROSE_DESIGN = """## Architecture
The service is composed of ten modules: an HTTP router, an auth middleware, a
user service, an order service, a payments adapter, a Postgres repository layer,
a Redis cache client, a background worker, a metrics exporter, and a CLI entry.
"""


@pytest.mark.parametrize("design", [_RAILS_DESIGN, _TREE_DESIGN, _PROSE_DESIGN],
                         ids=["other-language", "extension-less-tree", "prose"])
def test_large_design_triggers_manifest_even_when_file_regex_scores_zero(monkeypatch, design):
    monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "auto")
    monkeypatch.setattr(executor, "MANIFEST_FILE_THRESHOLD", 8)
    # the budget file counter misses these shapes entirely...
    assert executor.estimate_design_file_count(design) < 8
    # ...but the unit estimate catches them, so mode selection is correct.
    assert executor.estimate_design_unit_count(design) >= 8
    assert executor.use_manifest_mode(design) is True


@pytest.mark.parametrize("design", [
    "## Files\nonly/one.ts",                                  # one file
    "A small tool with two modules: a parser and a printer.",  # small prose
    "## Files\nsrc/a.rb\nsrc/b.rb\nGemfile",                   # 3 units, other lang
])
def test_small_design_stays_single_call(monkeypatch, design):
    monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "auto")
    monkeypatch.setattr(executor, "MANIFEST_FILE_THRESHOLD", 8)
    assert executor.use_manifest_mode(design) is False


def test_unit_count_does_not_disturb_budget_counter():
    """estimate_design_file_count (budget sizing) must be untouched by the
    separate mode-selection counter."""
    five_with_dup = "src/a.ts\nsrc/b.ts\nsrc/c.ts\nsrc/a.ts\nsrc/d.ts"
    assert executor.estimate_design_file_count(five_with_dup) == 4
    assert executor.estimate_design_file_count(
        "Some prose mentioning a function name but no file path at all") == 0


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


# ── parse_design_review (#3) ─────────────────────────────────────────────────

def test_parse_design_review_pass():
    raw = "<<<DESIGN_REVIEW>>>\nVERDICT: PASS\n<<<END_DESIGN_REVIEW>>>"
    assert executor.parse_design_review(raw) == ("PASS", "")


def test_parse_design_review_fail_carries_notes():
    raw = ("<<<DESIGN_REVIEW>>>\nVERDICT: FAIL\n"
           "1. count=20 collapses to 5 columns — state the distinct-column invariant.\n"
           "<<<END_DESIGN_REVIEW>>>")
    verdict, notes = executor.parse_design_review(raw)
    assert verdict == "FAIL"
    assert "distinct-column" in notes


def test_parse_design_review_failopen_on_garbage():
    # No verdict marker → fail-open to PASS so a malformed review never blocks.
    assert executor.parse_design_review("the design looks fine to me") == ("PASS", "")


def test_parse_design_review_tolerates_bracket_drift():
    raw = "<DESIGN_REVIEW>\nVERDICT: FAIL\nbroken\n<END_DESIGN_REVIEW>"
    verdict, notes = executor.parse_design_review(raw)
    assert verdict == "FAIL" and "broken" in notes
