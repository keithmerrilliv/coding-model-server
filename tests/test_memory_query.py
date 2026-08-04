"""DEV-489 — retrieve on the request, not on whatever fits in 256 tokens.

`get_context_string` embeds the last user message. That is right for chat and
wrong for the autonomous pipeline: those user messages lead with the entire spec
and design and end with the actual ask, and all-MiniLM-L6-v2 truncates its input
at 256 tokens (~1000 chars) — it discards the tail rather than averaging it. So
the ask never reached retrieval, and every file in a project would have
retrieved on the same spec preamble.

Measured: a 2,839-char spec.md is 693 tokens, of which 256 survive.

The fix lets a caller pass `memory_query`. These tests pin both halves: the
server prefers it, and the two callers that have a good query supply one.
"""
import asyncio
from unittest import mock

import pytest

from coding_model_autonomous import executor
from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage

# The limit that makes this bug possible. ~4 chars/token.
TRUNCATION_CHARS = 256 * 4


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _run(monkeypatch, memory, **kwargs):
    fake_mgr = mock.Mock()
    fake_mgr.ensure_running.return_value = None
    fake_mgr.tokenize.side_effect = lambda text: len(text or "") // 4
    fake_mgr.proxy_sync.return_value = {"id": "ok", "choices": []}
    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    monkeypatch.setattr(chat.runtime.services, "memory", memory, raising=False)

    request = ChatCompletionRequest(
        model="implementer", stream=False,
        messages=[ChatMessage(role="user", content="the whole spec goes here")],
        **kwargs)
    asyncio.run(chat.chat_completions(request, _FakeRequest()))
    return memory.get_context_string.call_args[0][0]


class TestServerPrefersExplicitQuery:
    def test_memory_query_is_used_when_given(self, monkeypatch):
        memory = mock.Mock()
        memory.get_context_string.return_value = "ctx"
        used = _run(monkeypatch, memory, memory_query="CentipedeChain.swift")
        assert used == "CentipedeChain.swift"

    def test_falls_back_to_last_user_message(self, monkeypatch):
        """Chat behavior must not change — nothing sets memory_query there."""
        memory = mock.Mock()
        memory.get_context_string.return_value = "ctx"
        used = _run(monkeypatch, memory)
        assert used == "the whole spec goes here"


class TestQueryBuilders:
    SPEC = (
        "# Make the stop button actually cancel MLX generation\n"
        "\n"
        "Tracks DEV-202. Do not create or transition any Jira issue.\n"
        "\n"
        "## Context\n"
        "\n"
        "Repo `electric-sheep`. Swift, macOS/visionOS SwiftUI app.\n"
    ) + ("Filler prose that must not reach the query. " * 200)

    def test_spec_query_leads_with_the_title(self):
        q = executor.spec_memory_query(self.SPEC)
        assert q.startswith("Make the stop button actually cancel MLX generation")

    def test_spec_query_survives_truncation(self):
        """The point of the exercise: it must fit in what the model reads."""
        assert len(executor.spec_memory_query(self.SPEC)) < TRUNCATION_CHARS

    def test_spec_query_stops_at_the_first_paragraph(self):
        assert "Filler prose" not in executor.spec_memory_query(self.SPEC)

    def test_spec_query_handles_a_spec_with_no_heading(self):
        assert executor.spec_memory_query("just prose\n") == "just prose"

    def test_spec_query_excludes_process_boilerplate(self):
        """DEV-494. Verbatim from spec_9872c963, which is how this was caught.

        The opening paragraph is process scaffolding, not subject matter, and
        against a corpus of Apple API docs that generic engineering prose is
        what retrieval matches: this spec pulled MTLPipelineOption and
        addComputePipelineFunctions into a design about mushroom fields, at
        distance 0.508.

        DEV-489's builders were verified with clean synthetic queries that had
        no boilerplate in them, which is exactly why it shipped. This case uses
        the real thing.
        """
        real = (
            "# Centipede — logic core, slice 1 (mushroom field, centipede "
            "movement, split-on-hit)\n"
            "\n"
            "This targets an **existing** repository, not a greenfield project. "
            "The Mac runner has it registered as `centipede` "
            "(`~/Dev/Metal/Centipede`, default branch `main`), and it already "
            "contains a working SwiftPM scaffold with a green test baseline:\n"
        )
        q = executor.spec_memory_query(real)
        assert q.startswith("Centipede")
        for leaked in ("SwiftPM", "greenfield", "Mac runner", "default branch",
                       "repository", "baseline"):
            assert leaked not in q, f"process boilerplate {leaked!r} reached the query"

    def test_spec_query_handles_empty_input(self):
        assert executor.spec_memory_query("") == ""

    def test_file_query_names_path_purpose_and_exports(self):
        entry = executor.ManifestEntry(
            path="Sources/CentipedeChain.swift",
            purpose="manages the centipede chain and its splits",
            exports="split(at:), advance()")
        q = executor.file_memory_query(entry)
        assert "CentipedeChain.swift" in q
        assert "splits" in q
        assert "split(at:)" in q
        assert len(q) < TRUNCATION_CHARS

    def test_file_query_tolerates_missing_exports(self):
        entry = executor.ManifestEntry(path="a.swift", purpose="does a thing")
        assert executor.file_memory_query(entry) == "a.swift does a thing"

    def test_different_files_produce_different_queries(self):
        """The actual defect: every file retrieving the same chunks."""
        a = executor.file_memory_query(
            executor.ManifestEntry(path="Renderer.swift", purpose="draws frames"))
        b = executor.file_memory_query(
            executor.ManifestEntry(path="Audio.swift", purpose="plays sound"))
        assert a != b


class TestCallAgentForwarding:
    def _call(self, monkeypatch, role, memory_roles, **kwargs):
        sent = {}

        def fake_post(agent, messages, **params):
            sent.update(params)
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {
                "choices": [{"message": {"content": "out"},
                             "finish_reason": "stop"}]}
            return resp

        monkeypatch.setattr(executor, "post_chat_completion", fake_post)
        monkeypatch.setattr(executor, "AUTONOMOUS_MEMORY_ROLES", memory_roles)
        executor.call_agent(role, [{"role": "user", "content": "hi"}], **kwargs)
        return sent

    def test_query_is_forwarded_when_the_role_opted_in(self, monkeypatch):
        sent = self._call(monkeypatch, "architect", {"architect"},
                          memory_query="a good query")
        assert sent.get("memory_query") == "a good query"
        assert sent.get("skip_memory") is False

    def test_query_is_omitted_when_the_role_did_not_opt_in(self, monkeypatch):
        """No point shipping a query the server will ignore."""
        sent = self._call(monkeypatch, "implementer", {"architect"},
                          memory_query="a good query")
        assert "memory_query" not in sent
        assert sent.get("skip_memory") is True

    def test_absent_query_is_not_sent_as_none(self, monkeypatch):
        sent = self._call(monkeypatch, "architect", {"architect"})
        assert "memory_query" not in sent


@pytest.mark.parametrize("name", ["spec_memory_query", "file_memory_query"])
def test_builders_are_exported(name):
    """orchestrator_daemon calls these through the executor module."""
    assert callable(getattr(executor, name))
