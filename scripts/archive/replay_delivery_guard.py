"""DEV-810 archive replay: the old DEV-756 guard vs assess_base, over every archived
delivery and every pipeline branch. Run from the repo root: venv/bin/python scripts/replay_delivery_guard.py"""
import json, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, "src")
from coding_model_autonomous import delivery as D

REPOS = {"centipede": Path.home()/"Dev/Centipede", "electric-sheep": Path.home()/"Dev/ElectricSheep"}
def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout
def show(repo, ref, rel):
    r = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None

def old_refuses(main_src, ours):
    return bool(D.test_names(main_src) - D.test_names(ours))

def judge(repo, main_ref, files, ours_of, base_files, base_is_current):
    with tempfile.TemporaryDirectory() as t:
        mr, ws = Path(t)/"main", Path(t)/"ws"
        old = False
        for rel in files:
            m, o = show(repo, main_ref, rel), ours_of(rel)
            if o is None: continue
            (ws/rel).parent.mkdir(parents=True, exist_ok=True); (ws/rel).write_text(o)
            if m is not None:
                (mr/rel).parent.mkdir(parents=True, exist_ok=True); (mr/rel).write_text(m)
                if old_refuses(m, o): old = True
        a = D.assess_base(mr, files, ws, base_files, base_is_current)
        head = (a.refusal or "").splitlines()[0][:70] if a.refusal else ""
        return old, a, head

print("== A: archived deliveries (main at delivery sha, base from the record)")
for rec_path in sorted(Path("var/tasks_db/specs").glob("*/delivery_base.json")):
    sd = rec_path.parent; rec = json.loads(rec_path.read_text())
    repo = REPOS["centipede"] if (sd/"Tests").exists() or (sd/"Sources").exists() else REPOS["electric-sheep"]
    _, base_files = D._base_from_context(sd)
    old, a, head = judge(repo, rec["default_branch_sha_at_delivery"], rec["files"],
                         lambda rel: (sd/rel).read_text(errors="replace") if (sd/rel).is_file() else None,
                         base_files, rec.get("base_is_current"))
    print(f"{sd.name} {repo.name:13} cur={rec.get('base_is_current')!s:5} old={'REFUSE' if old else 'ok':6} new={'REFUSE' if a.refusal else 'ok':6} renames={len(a.renames)} {head}")

print("\n== B: every pipeline branch vs today's origin/main")
for name, repo in REPOS.items():
    main_head = git(repo, "rev-parse", "origin/main").strip()
    for br in git(repo, "branch", "-r").split():
        if "pipeline/" not in br: continue
        mb = git(repo, "merge-base", br, "origin/main").strip()
        files = [f for f in git(repo, "diff", "--name-only", f"{mb}..{br}").split() if f]
        base_files = {f: show(repo, mb, f) for f in files if show(repo, mb, f) is not None}
        cur = (mb == main_head)
        old, a, head = judge(repo, "origin/main", files, lambda rel: show(repo, br, rel), base_files, cur)
        print(f"{br:42} cur={cur!s:5} old={'REFUSE' if old else 'ok':6} new={'REFUSE' if a.refusal else 'ok':6} renames={len(a.renames)} {head}")
