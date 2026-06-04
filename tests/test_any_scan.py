"""Tests for the deterministic `any`-type scan (fix C).

The reviewer's naive substring grep false-FAILed spec_5a87fd64 on the word "any"
in comments and string literals. These pin that the scanner ignores those and
catches only the real `any` TYPE.
"""
from qwen_autonomous import executor
from qwen_autonomous.executor import find_any_type_violations, scan_any_violations


# ── the exact false positives from spec_5a87fd64 → ZERO violations ───────────

FALSE_POSITIVES = [
    "// Loaded FIRST before any app code to ensure safe execution.",
    "// No `any` used — strict typing via core-js module declarations.",
    " * If any network error occurs or invalid data is received, falls back to",
    " * Requires any HDR transfer function (HLG or PQ) and WebGL >= 1.0.",
    "    id: 'hdr.any',",
    "  * HDR capability check: require any of the listed transfer functions.",
    "const anything = 1;",                 # identifier containing 'any'
    "const x = hdr.any;",                  # property access .any
    'const s = "cast as any in a string";',  # 'any' inside a string literal
]


def test_false_positives_are_not_flagged():
    for line in FALSE_POSITIVES:
        assert find_any_type_violations(line) == [], f"false positive on: {line}"


def test_block_comment_and_template_across_lines_ignored():
    src = (
        "/* this accepts any\n"
        "   value at all */\n"
        "const tpl = `name: any here`;\n"
        "const n: number = 1;\n"
    )
    assert find_any_type_violations(src) == []


# ── real `any` TYPE usages → flagged ─────────────────────────────────────────

REAL = [
    "const x: any = 1;",
    "const y = foo as any;",
    "const i = ROBUSTNESS_LADDER.indexOf(predicate.minRobustness as any);",
    "function f(arg: any): void {}",
    "let a: any[];",
    "const m: Record<string, any> = {};",
    "type U = string | any;",
    "const arr: Array<any> = [];",
]


def test_real_any_types_are_flagged():
    for line in REAL:
        assert len(find_any_type_violations(line)) == 1, f"missed real any in: {line}"


def test_reports_correct_line_number_and_text():
    src = "const ok = 1;\n// any in comment\nconst bad: any = 2;\n"
    hits = find_any_type_violations(src)
    assert len(hits) == 1
    assert hits[0][0] == 3                       # line number
    assert "bad: any" in hits[0][1]              # original text


# ── scan_any_violations over files ───────────────────────────────────────────

def test_scan_filters_extensions_and_formats():
    files = [
        ("ParamountDemo/server/resolver.ts", "const x = a as any;\n"),
        ("ParamountDemo/client/main.ts", "// just any comment\nconst n: number = 1;\n"),
        ("ParamountDemo/types.d.ts", "declare const z: any;\n"),   # .d.ts skipped
        ("ParamountDemo/package.json", '{"x": "any"}'),            # non-TS skipped
    ]
    out = scan_any_violations(files)
    assert out == ["ParamountDemo/server/resolver.ts:1: const x = a as any;"]


# ── reviewer message injects the authoritative scan ──────────────────────────

def test_reviewer_message_includes_clean_scan_directive():
    msgs = executor.build_reviewer_message(
        "SPEC", "DESIGN", [("a.ts", "// mentions any\nconst n: number = 1;\n")])
    u = msgs[1]["content"]
    assert "Authoritative `any`-type scan" in u
    assert "Do NOT FAIL" in u


def test_reviewer_message_lists_real_violations():
    msgs = executor.build_reviewer_message(
        "SPEC", "DESIGN", [("a.ts", "const x: any = 1;\n")])
    u = msgs[1]["content"]
    assert "a.ts:1:" in u
    assert "do NOT substring-search" in u
