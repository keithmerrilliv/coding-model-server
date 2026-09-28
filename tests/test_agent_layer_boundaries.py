"""DEV-839 scope 2 — the agent layer's module boundaries hold.

executor.py is a re-exporting façade over settings, prompts, _http, parsers,
messages and normalize. Two mistakes would each make a test pass while
testing nothing:

* Patching a name on the façade rebinds only the façade's copy. The module
  that reads the value never sees the patch. When the split first ran, the
  seam tier's fake model server was installed on the façade, so every seam
  test sent real completions to the live inference server.
* From-importing an env knob out of settings copies its value at import, so
  a patch on settings never reaches that reader. Knobs are read as
  ``settings.X`` at call time.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "coding_model_autonomous"
TESTS = ROOT / "tests"

_FACADE_ALIAS = re.compile(
    r"^\s*(?:from coding_model_autonomous import [^\n]*\bexecutor\b(?: as (\w+))?"
    r"|import coding_model_autonomous\.executor as (\w+)"
    r"|from coding_model_autonomous import executor as (\w+))", re.M)


def _knobs() -> set[str]:
    tree = ast.parse((PKG / "settings.py").read_text())
    names = set()
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id.lstrip("_").isupper():
                names.add(t.id)
    return names


def test_no_test_patches_the_executor_facade():
    offenders = []
    for path in TESTS.rglob("*.py"):
        if path == Path(__file__):
            continue
        src = path.read_text()
        aliases = {"executor"}
        for m in _FACADE_ALIAS.finditer(src):
            aliases.update(g for g in m.groups() if g)
        for alias in aliases:
            pat = rf"(?:setattr|patch\.object)\(\s*(?:\w+\.)?{re.escape(alias)}\s*,"
            if re.search(pat, src):
                offenders.append(f"{path.relative_to(ROOT)} patches `{alias}`")
        if re.search(r"[\"']coding_model_autonomous\.executor\.\w+[\"']", src):
            offenders.append(f"{path.relative_to(ROOT)} patches a dotted executor path")
    assert not offenders, (
        "patch the module that reads the name — settings for a knob, the home "
        "module for a function, the daemon for a name it imports:\n"
        + "\n".join(offenders))


def test_no_module_from_imports_a_knob():
    knobs = _knobs()
    assert knobs, "found no knobs in settings.py — the scan is broken"
    offenders = []
    for path in list(PKG.glob("*.py")) + [
            ROOT / "src" / "coding_model_server" / "orchestrator_daemon.py"]:
        if path.name in ("settings.py", "executor.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and \
                    node.module.endswith("settings"):
                leaked = [a.name for a in node.names if a.name in knobs]
                # MAX_RETRIES is the one knob the daemon takes by value; it
                # predates the split and nothing patches it at run time.
                leaked = [n for n in leaked if n != "MAX_RETRIES"]
                if leaked:
                    offenders.append(f"{path.name}: {leaked}")
    assert not offenders, "read knobs as settings.X at call time:\n" + "\n".join(offenders)


def test_the_facade_defines_nothing_of_its_own():
    tree = ast.parse((PKG / "executor.py").read_text())
    defined = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    assert not defined, "executor.py only re-exports; put new code in its home module"
