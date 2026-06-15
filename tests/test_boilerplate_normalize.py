"""Tests for deterministic boilerplate normalization (#3): exact-pin deps."""
import json

from coding_model_autonomous import executor


def test_pin_version_strips_ranges_keeps_exact_and_specials():
    assert executor.pin_version("^1.2.3") == "1.2.3"
    assert executor.pin_version("~1.2.3") == "1.2.3"
    assert executor.pin_version(">=1.2.3") == "1.2.3"
    assert executor.pin_version(" ^4.9.0 ") == "4.9.0"
    assert executor.pin_version("1.2.3") == "1.2.3"        # already exact
    assert executor.pin_version("*") == "*"                # wildcard left alone
    assert executor.pin_version("workspace:*") == "workspace:*"
    assert executor.pin_version("github:user/repo") == "github:user/repo"


def test_normalize_pins_package_json_deps():
    pkg = json.dumps({
        "name": "demo",
        "dependencies": {"shaka-player": "^4.9.0", "core-js": "3.49.0"},
        "devDependencies": {"esbuild": ">=0.28.0", "typescript": "~5.9.3"},
    })
    files = [("ParamountDemo/package.json", pkg), ("src/app.ts", "const x = 1;")]
    out, notes = executor.normalize_boilerplate(files)
    out_map = dict(out)
    data = json.loads(out_map["ParamountDemo/package.json"])
    assert data["dependencies"]["shaka-player"] == "4.9.0"   # ^ stripped
    assert data["dependencies"]["core-js"] == "3.49.0"        # already exact
    assert data["devDependencies"]["esbuild"] == "0.28.0"     # >= stripped
    assert data["devDependencies"]["typescript"] == "5.9.3"   # ~ stripped
    assert out_map["src/app.ts"] == "const x = 1;"            # non-pkg untouched
    assert len(notes) == 1 and "3 dependency range" in notes[0]


def test_normalize_noop_when_already_pinned():
    pkg = json.dumps({"dependencies": {"a": "1.0.0", "b": "2.3.4"}})
    out, notes = executor.normalize_boilerplate([("package.json", pkg)])
    assert notes == []
    assert dict(out)["package.json"] == pkg  # unchanged byte-for-byte


def test_normalize_leaves_invalid_json_for_reviewer():
    bad = '{ "dependencies": { "a": "^1.0.0",, } }'  # invalid JSON
    out, notes = executor.normalize_boilerplate([("package.json", bad)])
    assert notes == []
    assert dict(out)["package.json"] == bad
