"""The implementer is given the files it must modify (DEV-492).

Before this, `build_implementer_message` took spec + design + notes and nothing
else, so a spec saying "change line 163" produced a whole-file reconstruction
invented from the spec alone — and the runner wrote that invention over the
real file. These tests pin the three halves: the prompt carries the contents,
the client fails soft when the runner cannot serve them, and the orchestrator
only asks when there is something to ask for.
"""
import requests

from coding_model_autonomous import test_runner
from coding_model_autonomous.executor import (
    EXISTING_FILES_MAX_CHARS,
    build_implementer_message,
)


# ── the prompt ───────────────────────────────────────────────────────────────

def _user_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def test_existing_files_appear_with_preservation_framing():
    messages = build_implementer_message(
        "spec", "design",
        existing_files=[("A/B.swift", "struct Keep {}\nstruct AlsoKeep {}\n")],
    )
    text = _user_text(messages)
    assert "A/B.swift" in text
    assert "struct AlsoKeep {}" in text
    # The framing is load-bearing: four separate agents regenerated a file and
    # dropped its neighbours during the DEV-208 run.
    assert "ground truth" in text
    assert "deleted from the repository" in text


def test_absent_existing_files_leaves_the_prompt_unchanged():
    """Greenfield specs must be byte-identical to the pre-DEV-492 prompt."""
    before = _user_text(build_implementer_message("spec", "design"))
    after = _user_text(build_implementer_message("spec", "design", existing_files=[]))
    assert before == after
    assert "Current contents" not in before


def test_supplied_files_do_not_reuse_the_response_delimiter():
    """Echoing the <<<FILE:…>>> output format back as input invites the model to
    treat these as already-emitted.

    Scoped to the supplied-files block: the task instruction still names that
    delimiter, correctly, because it is the format the model must produce.
    """
    from coding_model_autonomous.executor import _render_existing_files
    block = _render_existing_files([("A.swift", "struct X {}")])
    assert "<<<FILE:" not in block
    assert "<<<END_FILE" not in block
    assert "A.swift" in block and "struct X {}" in block


def test_oversized_file_is_named_rather_than_silently_dropped():
    big = "x" * (EXISTING_FILES_MAX_CHARS + 1)
    text = _user_text(build_implementer_message(
        "spec", "design",
        existing_files=[("Small.swift", "ok"), ("Huge.swift", big)],
    ))
    assert "Small.swift" in text
    assert "Not shown" in text and "Huge.swift" in text
    # It must be told it has NOT seen the file, or it will emit one anyway.
    assert "Do not emit them" in text


# ── the client ───────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_fetch_returns_files_and_reports_unreadable_ones(monkeypatch):
    monkeypatch.setattr(test_runner._SESSION, "post", lambda *a, **k: _Resp(200, {
        "files": [
            {"path": "A.swift", "content": "struct A {}"},
            {"path": "Gone.swift", "content": None, "error": "does not exist"},
        ]
    }))
    files, problems = test_runner.fetch_repo_files("proj", ["A.swift", "Gone.swift"])
    assert files == [("A.swift", "struct A {}")]
    assert problems == ["Gone.swift: does not exist"]


def test_old_runner_without_the_route_fails_soft(monkeypatch):
    """Deploy order must not matter: an un-redeployed Mac degrades to the old
    behaviour rather than failing the spec."""
    monkeypatch.setattr(test_runner._SESSION, "post", lambda *a, **k: _Resp(404, text="nope"))
    files, problems = test_runner.fetch_repo_files("proj", ["A.swift"])
    assert files == []
    assert "no /v1/read_files route" in problems[0]


def test_unreachable_runner_fails_soft(monkeypatch):
    """The Mac link drops regularly (DEV-518); a read failure must not be fatal."""
    def boom(*a, **k):
        raise requests.RequestException("connection reset")
    monkeypatch.setattr(test_runner._SESSION, "post", boom)
    files, problems = test_runner.fetch_repo_files("proj", ["A.swift"])
    assert files == []
    assert "could not reach" in problems[0]


def test_no_paths_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not have dispatched")
    monkeypatch.setattr(test_runner._SESSION, "post", boom)
    assert test_runner.fetch_repo_files("proj", []) == ([], [])


# ── the gate on asking at all ────────────────────────────────────────────────

def _spec(yaml_text):
    class _S:
        id = "spec_test"
        normalized_yaml = yaml_text
    return _S()


MODIFY_SPEC = """
| path | change |
| `ElectricSheep/ForcingStrategy.swift` | modify (line 120) |
"""


def test_greenfield_spec_asks_for_nothing(monkeypatch):
    from coding_model_server import orchestrator_daemon as od
    monkeypatch.setattr(test_runner, "fetch_repo_files",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert od._fetch_existing_files_for_spec(
        _spec("test_strategy:\n  repo: proj\n"), "no table here") == []


def test_local_framework_without_a_repo_asks_for_nothing(monkeypatch):
    """pytest/node specs have no runner-side checkout to read from."""
    from coding_model_server import orchestrator_daemon as od
    monkeypatch.setattr(test_runner, "fetch_repo_files",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert od._fetch_existing_files_for_spec(
        _spec("test_strategy:\n  framework: pytest\n"), MODIFY_SPEC) == []


def test_modify_spec_with_a_repo_fetches_at_base_ref(monkeypatch):
    from coding_model_server import orchestrator_daemon as od
    seen = {}

    def fake(repo, paths, base_ref):
        seen.update(repo=repo, paths=paths, base_ref=base_ref)
        return [("ElectricSheep/ForcingStrategy.swift", "struct A {}")], []

    monkeypatch.setattr(test_runner, "fetch_repo_files", fake)
    out = od._fetch_existing_files_for_spec(
        _spec("test_strategy:\n  repo: electric-sheep\n  base_ref: abc123\n"),
        MODIFY_SPEC,
    )
    assert out == [("ElectricSheep/ForcingStrategy.swift", "struct A {}")]
    assert seen["repo"] == "electric-sheep"
    assert seen["base_ref"] == "abc123"
    assert seen["paths"] == ["ElectricSheep/ForcingStrategy.swift"]


def test_a_read_failure_never_kills_the_spec(monkeypatch):
    from coding_model_server import orchestrator_daemon as od

    def boom(*a, **k):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(test_runner, "fetch_repo_files", boom)
    assert od._fetch_existing_files_for_spec(
        _spec("test_strategy:\n  repo: proj\n"), MODIFY_SPEC) == []


# ── the plan gate no longer blocks what it can now read ──────────────────────

def test_plan_gate_allows_a_modify_spec_once_the_files_are_readable(monkeypatch):
    """The DEV-492 guard was a hard stop because the pipeline could not read
    files. Now that it can, the guard must not block the very specs the read
    path exists to enable."""
    from coding_model_server import orchestrator_daemon as od
    monkeypatch.setattr(
        test_runner, "fetch_repo_files",
        lambda repo, paths, base_ref: ([(p, "content") for p in paths], []))
    assert od._unreadable_declared_modifications(
        _spec(""), "test_strategy:\n  repo: electric-sheep\n", MODIFY_SPEC) == []


def test_plan_gate_still_blocks_when_a_file_cannot_be_read(monkeypatch):
    """The original hazard is unchanged: refuse rather than overwrite blind."""
    from coding_model_server import orchestrator_daemon as od
    monkeypatch.setattr(test_runner, "fetch_repo_files",
                        lambda repo, paths, base_ref: ([], ["gone"]))
    assert od._unreadable_declared_modifications(
        _spec(""), "test_strategy:\n  repo: electric-sheep\n", MODIFY_SPEC
    ) == ["ElectricSheep/ForcingStrategy.swift"]


def test_plan_gate_still_blocks_without_a_registered_repo():
    from coding_model_server import orchestrator_daemon as od
    assert od._unreadable_declared_modifications(
        _spec(""), "test_strategy:\n  framework: pytest\n", MODIFY_SPEC
    ) == ["ElectricSheep/ForcingStrategy.swift"]


def test_plan_gate_ignores_greenfield_specs():
    from coding_model_server import orchestrator_daemon as od
    assert od._unreadable_declared_modifications(
        _spec(""), "test_strategy:\n  repo: electric-sheep\n", "no table") == []
