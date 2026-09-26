"""DEV-535 — a fully approved run must leave the change on a branch of the
target repo, or say plainly that it did not.

Four verified runs ended with Jira Done and a byte-identical target repo;
the only surviving copy of each change was scratch state under var/. These
tests drive the delivery step against a real local bare repo so the git
mechanics (clone, branch, commit, force-push) are exercised for real.
"""
import json
import subprocess

import pytest

from coding_model_autonomous import delivery


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


@pytest.fixture
def remote(tmp_path):
    """A bare 'origin' seeded with one commit on its default branch."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), "seed")
    (seed / "README.md").write_text("hello\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    _git(seed, "push", "origin", "HEAD")
    return bare


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec_ws"
    (d / "Sources").mkdir(parents=True)
    (d / "Sources" / "Thing.swift").write_text("struct Thing {}\n")
    (d / "Sources" / "Protected.swift").write_text("MUST NOT SHIP\n")
    return d


def test_pushes_code_artifacts_to_a_pipeline_branch(remote, spec_dir, tmp_path,
                                                    monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    r = delivery.deliver_spec(
        "spec_t1", "Demo spec", spec_dir,
        ["Sources/Thing.swift", "Sources/Protected.swift"],
        repo_name="demo", protected_paths=["Sources/Protected.swift"])
    assert r.status == "pushed", r.detail
    assert r.branch == "pipeline/spec_t1"

    check = tmp_path / "check"
    _git(tmp_path, "clone", "--branch", "pipeline/spec_t1", str(remote), "check")
    assert (check / "Sources" / "Thing.swift").read_text() == "struct Thing {}\n"
    assert not (check / "Sources" / "Protected.swift").exists(), \
        "protected paths must never be delivered"
    log = _git(check, "log", "-1", "--format=%an %s")
    assert "coding-model-pipeline" in log and "spec_t1" in log


def test_redelivery_force_updates_the_pipeline_branch(remote, spec_dir, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    args = ("spec_t1", "Demo spec", spec_dir, ["Sources/Thing.swift"], "demo", [])
    assert delivery.deliver_spec(*args).status == "pushed"
    (spec_dir / "Sources" / "Thing.swift").write_text("struct Thing { let v = 2 }\n")
    r = delivery.deliver_spec(*args)
    assert r.status == "pushed", r.detail
    _git(tmp_path, "clone", "--branch", "pipeline/spec_t1", str(remote), "check2")
    assert "v = 2" in (tmp_path / "check2" / "Sources" / "Thing.swift").read_text()


def test_no_repo_name_is_an_honest_skip(spec_dir):
    r = delivery.deliver_spec("spec_t2", "Greenfield", spec_dir,
                              ["Sources/Thing.swift"], None, [])
    assert r.status == "skipped"
    assert "workspace" in r.detail


def test_unconfigured_remote_is_an_honest_skip(spec_dir, monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_REMOTES", raising=False)
    r = delivery.deliver_spec("spec_t3", "Demo", spec_dir,
                              ["Sources/Thing.swift"], "demo", [])
    assert r.status == "skipped"
    assert "AUTONOMOUS_DELIVERY_REMOTES" in r.detail


def test_content_already_on_default_branch_skips(remote, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "README.md").write_text("hello\n")  # identical to the seed commit
    r = delivery.deliver_spec("spec_t4", "Demo", ws, ["README.md"], "demo", [])
    assert r.status == "skipped"
    assert "byte-for-byte" in r.detail


def test_unreachable_remote_fails_open(spec_dir, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES",
                       "demo=/nonexistent/nowhere.git")
    r = delivery.deliver_spec("spec_t5", "Demo", spec_dir,
                              ["Sources/Thing.swift"], "demo", [])
    assert r.status == "failed"
    assert "clone" in r.detail


def test_remotes_parser_handles_multiple_pairs(monkeypatch):
    monkeypatch.setenv(
        "AUTONOMOUS_DELIVERY_REMOTES",
        "electric-sheep=git@github.com:k/ES.git, centipede=git@github.com:k/C.git")
    remotes = delivery.delivery_remotes()
    assert remotes == {"electric-sheep": "git@github.com:k/ES.git",
                       "centipede": "git@github.com:k/C.git"}


# ── DEV-756: a stale snapshot is refused, and the base is recorded ───────────

def _seed_test_file(remote, tmp_path, name, content):
    seed = tmp_path / f"seed_{name}"
    _git(tmp_path, "clone", str(remote), seed.name)
    (seed / "Tests").mkdir(exist_ok=True)
    (seed / "Tests" / "GameTests.swift").write_text(content)
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", name)
    _git(seed, "push", "origin", "HEAD")


def test_names_extraction_covers_both_swift_styles_and_pytest():
    from coding_model_autonomous.delivery import test_names
    src = ("func testA() {}\n@Test func b() {}\n@Test(\"x\") func c() throws {}\n"
           "// func testCommented() {}\nfunc helper() {}\ndef test_py():\n    pass\n")
    assert test_names(src) == {"testA", "b", "c", "test_py"}


def test_delivery_refuses_a_snapshot_that_removes_tests_main_has(remote, spec_dir, tmp_path,
                                                                 monkeypatch):
    from coding_model_autonomous import delivery
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    _seed_test_file(remote, tmp_path, "main_has_two",
                    "func testOne() {}\nfunc testTwo() {}\n")
    (spec_dir / "Tests").mkdir(exist_ok=True)
    (spec_dir / "Tests" / "GameTests.swift").write_text("func testOne() {}\nfunc testNew() {}\n")
    r = delivery.deliver_spec("spec_t2", "slice", spec_dir,
                              ["Tests/GameTests.swift"], repo_name="demo", protected_paths=[])
    assert r.status == "failed"
    # Same test count, no recorded base: refused, but named as a probable
    # rename on an unverified base rather than a stale one (DEV-810).
    assert "REFUSED" in r.detail and "testTwo" in r.detail
    assert "probable rename" in r.detail and "stale base" not in r.detail
    rec = json.loads((spec_dir / delivery.DELIVERY_BASE).read_text())
    assert rec["refused"] is True and rec["files"] == ["Tests/GameTests.swift"]
    # nothing reached the remote
    assert "pipeline/spec_t2" not in _git(tmp_path, "ls-remote", "--heads", str(remote))


def test_delivery_that_only_adds_tests_pushes_and_records_the_base(remote, spec_dir, tmp_path,
                                                                    monkeypatch):
    from coding_model_autonomous import delivery
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    _seed_test_file(remote, tmp_path, "main_has_one", "func testOne() {}\n")
    (spec_dir / "Tests").mkdir(exist_ok=True)
    (spec_dir / "Tests" / "GameTests.swift").write_text("func testOne() {}\nfunc testNew() {}\n")
    r = delivery.deliver_spec("spec_t3", "slice", spec_dir,
                              ["Tests/GameTests.swift"], repo_name="demo", protected_paths=[])
    assert r.status == "pushed", r.detail
    assert "cut from default branch" in r.detail
    rec = json.loads((spec_dir / delivery.DELIVERY_BASE).read_text())
    assert rec["refused"] is False and rec["default_branch_sha_at_delivery"]
    assert rec["base_sha"] is None            # no context.json in this workspace


def test_main_moving_under_a_delivered_file_is_refused_when_the_base_is_known(tmp_path):
    from coding_model_autonomous.delivery import stale_base_refusal
    repo = tmp_path / "repo"; (repo / "Sources").mkdir(parents=True)
    (repo / "Sources" / "A.swift").write_text("let a = 2  // main moved\n")
    ws = tmp_path / "ws"; (ws / "Sources").mkdir(parents=True)
    (ws / "Sources" / "A.swift").write_text("let a = 1\nlet b = 9\n")
    base = {"Sources/A.swift": "let a = 1\n"}
    why = stale_base_refusal(repo, ["Sources/A.swift"], ws, base)
    assert why and "main changed since the base" in why
    # main == base: the artifact is a clean edit of what it read — no refusal
    (repo / "Sources" / "A.swift").write_text("let a = 1\n")
    assert stale_base_refusal(repo, ["Sources/A.swift"], ws, base) is None


# ── DEV-810: a rename is not a deletion, and a current base is not stale ─────

# Run 57's BoardAdapterTests, reduced to the declarations the guard reads.
_RUN57_MAIN = ("@Test func adapterMapsEveryDrawableKindAndDropsShot() {}\n"
               "@Test func adaptedBoardRendersWhereBoardSaid() {}\n")
_RUN57_OURS = ("@Test func adapterMapsEveryDrawableKindIncludingShotAndPoison() {}\n"
               "@Test func adaptedBoardRendersWhereBoardSaid() {}\n")
_RUN57_PATH = "Tests/CentipedeRenderTests/BoardAdapterTests.swift"


def _pair(tmp_path, main_src, ours_src, rel=_RUN57_PATH):
    repo, ws = tmp_path / "repo", tmp_path / "ws"
    for root, src in ((repo, main_src), (ws, ours_src)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(src)
    return repo, ws


def test_run57_rename_on_a_current_base_is_delivered_and_named(tmp_path):
    repo, ws = _pair(tmp_path, _RUN57_MAIN, _RUN57_OURS)
    a = delivery.assess_base(repo, [_RUN57_PATH], ws, {}, base_is_current=True)
    assert a.refusal is None
    assert len(a.renames) == 1
    assert "adapterMapsEveryDrawableKindAndDropsShot" in a.renames[0]
    assert "adapterMapsEveryDrawableKindIncludingShotAndPoison" in a.renames[0]
    assert "test count 2 → 2" in a.renames[0]


def test_a_rename_on_a_stale_base_is_refused_and_called_a_probable_rename(tmp_path):
    repo, ws = _pair(tmp_path, _RUN57_MAIN, _RUN57_OURS)
    why = delivery.stale_base_refusal(repo, [_RUN57_PATH], ws, {},
                                      base_is_current=False)
    assert why and why.startswith("REFUSED — stale base")
    assert "probable rename" in why
    assert "Re-run the spec against current main" in why


def test_a_rename_on_an_unrecorded_base_is_refused_without_calling_it_stale(tmp_path):
    repo, ws = _pair(tmp_path, _RUN57_MAIN, _RUN57_OURS)
    why = delivery.stale_base_refusal(repo, [_RUN57_PATH], ws, {})
    assert why and "base not recorded" in why
    assert "stale base" not in why
    assert "deliver by hand" in why


def test_a_real_deletion_on_a_current_base_is_refused_as_the_artifacts_own_change(
        tmp_path):
    """Names gone AND the count fell: refused on any base. On a current base
    the refusal must not say stale, and must not advise a re-run that would
    only reproduce it."""
    repo, ws = _pair(tmp_path, _RUN57_MAIN,
                     "@Test func adaptedBoardRendersWhereBoardSaid() {}\n")
    why = delivery.stale_base_refusal(repo, [_RUN57_PATH], ws, {},
                                      base_is_current=True)
    assert why and "stale base" not in why
    assert "removes 1 test(s) main has (test count 2 → 1)" in why
    assert "artifact's own change" in why
    assert "Re-run the spec against current main," not in why


def test_a_real_deletion_on_a_stale_base_is_still_called_stale(tmp_path):
    repo, ws = _pair(tmp_path, _RUN57_MAIN,
                     "@Test func adaptedBoardRendersWhereBoardSaid() {}\n")
    why = delivery.stale_base_refusal(repo, [_RUN57_PATH], ws, {},
                                      base_is_current=False)
    assert why and why.startswith("REFUSED — stale base")
    assert "removes 1 test(s)" in why


def test_run57_delivers_end_to_end_when_the_base_is_current(remote, spec_dir, tmp_path,
                                                           monkeypatch):
    """Through deliver_spec: the base recorded in context equals main's head,
    so run 57's rename is pushed and both name lists are on the record."""
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES", f"demo={remote}")
    seed = tmp_path / "seed57"
    _git(tmp_path, "clone", str(remote), seed.name)
    (seed / _RUN57_PATH).parent.mkdir(parents=True, exist_ok=True)
    (seed / _RUN57_PATH).write_text(_RUN57_MAIN)
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "run57 base")
    _git(seed, "push", "origin", "HEAD")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True,
                          text=True, check=True).stdout.strip()
    monkeypatch.setattr(delivery, "_base_from_context", lambda d: (head, {}))
    (spec_dir / _RUN57_PATH).parent.mkdir(parents=True, exist_ok=True)
    (spec_dir / _RUN57_PATH).write_text(_RUN57_OURS)

    r = delivery.deliver_spec("spec_57", "slice 3", spec_dir, [_RUN57_PATH],
                              repo_name="demo", protected_paths=[])

    assert r.status == "pushed", r.detail
    assert "(current)" in r.detail and "renamed on a current base" in r.detail
    rec = json.loads((spec_dir / delivery.DELIVERY_BASE).read_text())
    assert rec["base_is_current"] is True and rec["refused"] is False
    assert "adapterMapsEveryDrawableKindAndDropsShot" in rec["renamed_tests"][0]
