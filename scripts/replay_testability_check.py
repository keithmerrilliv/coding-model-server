"""DEV-809 archive replay: the design testability/completeness checks on main vs
the working tree, over every design in var/tasks_db/specs (retry history included).
Prints the counts per finding kind and every finding that appears or disappears.
Run from the repo root: venv/bin/python scripts/replay_testability_check.py [ref]"""
import collections, importlib.util, re, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, "src")
REF = sys.argv[1] if len(sys.argv) > 1 else "main"
MOD = "src/coding_model_autonomous/design_testability.py"


def load(name, source):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(source)
    spec = importlib.util.spec_from_file_location(
        f"coding_model_autonomous.{name}", f.name,
        submodule_search_locations=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "coding_model_autonomous"
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


old = load("dt_old", subprocess.run(["git", "show", f"{REF}:{MOD}"], capture_output=True,
                                    text=True, check=True).stdout)
new = load("dt_new", Path(MOD).read_text())


def _subject(f):
    """The criterion, or for type/file findings (criterion "") the named type."""
    if f.criterion:
        return f.criterion[:60]
    m = re.search(r"`([^`]+)`", f.detail)
    return m.group(1) if m else ""


def findings(mod, md):
    return {(f.kind, _subject(f)) for f in
            mod.check_design_testability(md) + mod.check_design_completeness(md)}


designs = sorted(Path("var/tasks_db/specs").rglob("design*.md"))
tot_old, tot_new = collections.Counter(), collections.Counter()
gone, came = [], []
for p in designs:
    md = p.read_text(errors="replace")
    a, b = findings(old, md), findings(new, md)
    tot_old.update(k for k, _ in a); tot_new.update(k for k, _ in b)
    gone += [(str(p), f) for f in sorted(a - b)]
    came += [(str(p), f) for f in sorted(b - a)]
print(f"{len(designs)} designs replayed against {REF}")
for k in sorted(set(tot_old) | set(tot_new)):
    print(f"  {k:28} {tot_old[k]:5} -> {tot_new[k]:5}")
print(f"\nDISAPPEARED ({len(gone)}):")
for p, f in gone: print("  ", p.replace("var/tasks_db/specs/", ""), f)
print(f"\nAPPEARED ({len(came)}):")
for p, f in came: print("  ", p.replace("var/tasks_db/specs/", ""), f)
