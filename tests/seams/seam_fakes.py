"""Fakes for the seam tier (DEV-634): a scripted model server and a scripted runner.

The daemon reaches the outside world through two seams and the tier fakes
exactly those, one level BELOW the daemon's own code so its handling runs for
real:

* ``post_chat_completion`` — the HTTP call under ``executor.call_agent``,
  ``planner.call_planner`` and ``supervisor.decide``. The fake returns a
  Response stand-in, so ``call_agent`` still does its own ``raise_for_status``,
  usage/finish_reason bookkeeping and truncation warning. Faults are the ones
  live runs produced: dead transport, timeout, HTTP 413/502, empty content, an
  unclosed think block, ``finish_reason=length``, a body with no choices.
* ``test_runner.fetch_repo_files`` and ``run_tests`` — the Mac runner and the
  sandbox. The fake returns the same tuples and the same diagnostic strings the
  real ones do, so ``problems_indicate_runner_outage``, ``is_runner_unreachable``,
  ``_detect_build_failure`` and the structural output guard all run unmodified.

Nothing here logs, sleeps or touches the network. Every call is recorded so a
test can assert what the daemon asked for, not only what it did afterwards.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor, planner, supervisor

# ── role detection ───────────────────────────────────────────────────────────
# Each role's system prompt opens with a fixed sentence; the fake keys its
# script on that rather than on the agent name, because the planner and the
# architect share an agent and the design reviewer borrows the reviewer's.
ROLE_NEEDLES: list[tuple[str, str]] = [
    ("planner", "You are the PLANNER"),
    ("design_review", "You are a DESIGN REVIEWER"),
    ("architect", "You are the ARCHITECT"),
    ("manifest", "You are the IMPLEMENTER in its planning step"),
    ("per_file", "You are the IMPLEMENTER writing ONE file"),
    ("implementer", "You are the IMPLEMENTER agent"),
    ("reviewer", "You are the REVIEWER"),
    ("synthesis", "You are a code-synthesis agent"),
    ("supervisor", "SUPERVISOR"),
]


def role_of(messages: list[dict]) -> str:
    text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    for role, needle in ROLE_NEEDLES:
        if needle in text:
            return role
    return "unknown"


# ── scripted replies and faults ──────────────────────────────────────────────

@dataclass
class Reply:
    """A 200 with content. ``finish_reason="length"`` marks a truncation."""
    content: str
    finish_reason: str = "stop"
    prompt_tokens: int = 1000
    completion_tokens: int = 200


def Truncated(content: str) -> Reply:
    return Reply(content, finish_reason="length")


def Empty() -> Reply:
    """A 200 whose visible content is empty (the DEV-543 / DEV-617 shape)."""
    return Reply("", completion_tokens=8000)


def UnclosedThink() -> Reply:
    """Tokens spent inside a think block that never closes; no visible answer."""
    return Reply("<think>\nLet me consider the design carefully. Wait!\nI am stuck",
                 completion_tokens=8000)


@dataclass
class Refuse:
    """An HTTP error status. 413 = prompt too large; 502 = server mid-crash."""
    status: int = 413


@dataclass
class Down:
    """Transport failure: requests.ConnectionError."""


@dataclass
class Hang:
    """Transport failure: requests.Timeout."""


@dataclass
class MissingChoices:
    """A 200 whose body has no choices — call_agent raises RuntimeError."""


class UnscriptedCall(RuntimeError):
    """The daemon asked a role the test did not script. Recorded AND raised so
    the daemon's own handling runs, then the harness fails the test loudly."""


@dataclass
class ModelCall:
    role: str
    model: str
    max_tokens: Any
    served: Any
    messages: list[dict] = field(repr=False, default_factory=list)


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            kind = "Client" if self.status_code < 500 else "Server"
            raise requests.HTTPError(
                f"{self.status_code} {kind} Error: fake for url: http://fake/v1/chat/completions",
                response=self)  # type: ignore[arg-type]


class FakeModelServer:
    """Scripted stand-in for the inference API, keyed by role.

    ``script(role, *items)`` queues replies for that role in call order;
    ``always(role, item)`` sets what to serve once the queue is empty. An item
    is a Reply/fault or a callable ``(messages) -> item`` for content that must
    depend on the prompt. An unscripted role raises UnscriptedCall.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, deque] = {}
        self.defaults: dict[str, Any] = {}
        self.calls: list[ModelCall] = []
        self.unscripted: list[str] = []

    def script(self, role: str, *items: Any) -> "FakeModelServer":
        self.scripts.setdefault(role, deque()).extend(items)
        return self

    def always(self, role: str, item: Any) -> "FakeModelServer":
        self.defaults[role] = item
        return self

    def install(self, monkeypatch) -> "FakeModelServer":
        for mod in (executor, planner, supervisor):
            monkeypatch.setattr(mod, "post_chat_completion", self.post)
        return self

    def calls_for(self, role: str) -> list[ModelCall]:
        return [c for c in self.calls if c.role == role]

    # the seam itself ---------------------------------------------------------
    def post(self, model, messages, *, timeout=None, skip_memory=True,
             retry_5xx=False, **params):
        role = role_of(messages)
        queue = self.scripts.get(role)
        if queue:
            item = queue.popleft()
        elif role in self.defaults:
            item = self.defaults[role]
        else:
            self.unscripted.append(role)
            self.calls.append(ModelCall(role, model, params.get("max_tokens"), None, messages))
            raise UnscriptedCall(f"unscripted {role} call (model={model})")
        if callable(item):
            item = item(messages)
        self.calls.append(ModelCall(role, model, params.get("max_tokens"), item, messages))
        if isinstance(item, Down):
            raise requests.ConnectionError("fake model server: connection refused")
        if isinstance(item, Hang):
            raise requests.Timeout("fake model server: read timed out")
        if isinstance(item, Refuse):
            return _FakeResponse(item.status, {"error": {"message": f"fake {item.status}"}})
        if isinstance(item, MissingChoices):
            return _FakeResponse(200, {"id": "fake", "choices": []})
        assert isinstance(item, Reply), f"unknown script item {item!r}"
        total = item.prompt_tokens + item.completion_tokens
        return _FakeResponse(200, {
            "id": "fake", "model": model,
            "choices": [{"index": 0, "finish_reason": item.finish_reason,
                         "message": {"role": "assistant", "content": item.content}}],
            "usage": {"prompt_tokens": item.prompt_tokens,
                      "completion_tokens": item.completion_tokens,
                      "total_tokens": total},
        })


# ── the runner and the sandbox ───────────────────────────────────────────────

@dataclass
class TestOutcome:
    """What one ``run_tests`` dispatch reports: (passed, output)."""
    passed: bool
    output: str
    name: str = ""


def PytestPass(n: int = 4) -> TestOutcome:
    body = "\n".join(f"tests/test_seam.py::test_{i} PASSED [{(i+1)*100//n:3d}%]" for i in range(n))
    return TestOutcome(True, (
        "============================= test session starts ==============================\n"
        f"collecting ... collected {n} items\n\n{body}\n\n"
        f"============================== {n} passed in 0.12s ==============================\n"), "pytest_pass")


def PytestFail(failed: int = 1, passed: int = 3) -> TestOutcome:
    return TestOutcome(False, (
        "============================= test session starts ==============================\n"
        f"collected {failed + passed} items\n\n"
        "tests/test_seam.py::test_0 FAILED\n"
        "E   AssertionError: assert 1 == 2\n\n"
        f"========================= {failed} failed, {passed} passed in 0.20s =========================\n"),
        "pytest_fail")


def CollectionError(module: str = "coding_model_server") -> TestOutcome:
    """The DEV-626 shape: the sandbox cannot import the package under test."""
    return TestOutcome(False, (
        "============================= test session starts ==============================\n"
        "collecting ... collected 0 items / 1 error\n\n"
        "==================================== ERRORS ====================================\n"
        "_______________ ERROR collecting tests/test_seam.py _______________\n"
        f"E   ModuleNotFoundError: No module named '{module}'\n"
        "=========================== short test summary info ============================\n"
        "ERROR tests/test_seam.py\n"
        "=============================== 1 error in 0.10s ===============================\n"),
        "collection_error")


def Unreachable() -> TestOutcome:
    """The runner did not answer — the DEV-538 requeue class."""
    return TestOutcome(False, (
        "mac-runner unreachable at http://127.0.0.1:5050/v1/run_tests: "
        "HTTPConnectionPool(host='127.0.0.1', port=5050): Read timed out. (read timeout=330)"),
        "unreachable")


def Inconclusive() -> TestOutcome:
    """Exit zero, no summary — the structural guard must refuse to call it a pass."""
    return TestOutcome(True, "sandbox: nothing to report\n", "inconclusive")


class FakeRunner:
    """Scripted stand-in for the Mac runner's read path and the test sandbox.

    ``repo_files`` is what ``fetch_repo_files`` can return; ``fetch_mode`` is
    ``"ok"``, ``"down"`` (the transport-class single problem the daemon parks
    on), or ``"raise"`` (a ConnectionError out of the call), or a callable of
    the call index. ``tests`` queues TestOutcomes for successive ``run_tests``
    dispatches; ``default_test`` serves when the queue is empty.
    """

    DOWN_PROBLEM = ("could not reach the runner's read path: HTTPConnectionPool("
                    "host='127.0.0.1', port=5050): Max retries exceeded with url: "
                    "/v1/read_files (Caused by NewConnectionError: [Errno 111] "
                    "Connection refused)")

    def __init__(self, repo_files: dict[str, str] | None = None) -> None:
        self.repo_files: dict[str, str] = dict(repo_files or {})
        self.fetch_mode: Any = "ok"
        self.fetch_calls: list[tuple[str, list[str], str]] = []
        self.tests: deque = deque()
        self.default_test: TestOutcome | Callable = PytestPass()
        self.test_calls: list[tuple[str, dict, TestOutcome]] = []

    def install(self, monkeypatch) -> "FakeRunner":
        monkeypatch.setattr(d.test_runner, "fetch_repo_files", self.fetch_repo_files)
        monkeypatch.setattr(d, "run_tests", self.run_tests)
        return self

    def then(self, *outcomes: Any) -> "FakeRunner":
        self.tests.extend(outcomes)
        return self

    def fetch_repo_files(self, repo, paths, base_ref="HEAD"):
        paths = list(paths)
        self.fetch_calls.append((repo, paths, base_ref))
        mode = self.fetch_mode(len(self.fetch_calls)) if callable(self.fetch_mode) else self.fetch_mode
        if mode == "raise":
            raise requests.ConnectionError("fake runner: connection refused")
        if mode == "down":
            return [], [self.DOWN_PROBLEM]
        files = [(p, self.repo_files[p]) for p in paths if p in self.repo_files]
        problems = [f"{p}: fatal: path '{p}' does not exist in '{base_ref}'"
                    for p in paths if p not in self.repo_files]
        return files, problems

    def run_tests(self, spec_dir, framework="pytest", timeout=None, **opts):
        item = self.tests.popleft() if self.tests else self.default_test
        if callable(item) and not isinstance(item, TestOutcome):
            item = item(spec_dir, framework, opts)
        self.test_calls.append((framework, dict(opts), item))
        return item.passed, item.output
