"""DEV-488 — RAG must reach callers who supply their own system message.

`_build_openai_messages` drops the agent system prompt whenever the client sends
its own, so a RAG block appended to that prompt goes off the wire with it.
DEV-350 responded by skipping retrieval entirely for those requests. That was a
correct optimization of work whose result could not ship, but it silently
nullified RAG for the *entire* autonomous pipeline: all 8 executor call sites
send their own system message, so retrieval was skipped before `skip_memory` was
ever read. `AUTONOMOUS_MEMORY_ROLES` became dead config — 9 architect calls
logged `rag=on` and none of them injected anything.

The fix delivers the block in the caller's own system message, the same way the
token-budget guidance already had to be delivered (a second system message is
not an option — strict Jinja templates reject it). `skip_memory` is now the
single control over whether retrieval runs.

These tests assert delivery ON THE WIRE, not that a flag propagated — mistaking
the latter for the former is what let this go unnoticed.
"""
import asyncio
from unittest import mock

from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage

MEMORY_TEXT = "MEMORY-BLOCK-SENTINEL"


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _run_chat(monkeypatch, messages, memory, **kwargs):
    fake_mgr = mock.Mock()
    fake_mgr.ensure_running.return_value = None
    fake_mgr.tokenize.side_effect = lambda text: len(text or "") // 4
    fake_mgr.proxy_sync.return_value = {"id": "ok", "choices": []}

    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    monkeypatch.setattr(chat.runtime.services, "memory", memory, raising=False)

    request = ChatCompletionRequest(
        model="implementer", messages=messages, stream=False, **kwargs)
    result = asyncio.run(chat.chat_completions(request, _FakeRequest()))
    # proxy_sync(messages, augmented_system, ...) — positional.
    sent_messages, sent_system = fake_mgr.proxy_sync.call_args[0][:2]
    return result, sent_messages, sent_system


def _fake_memory():
    memory = mock.Mock()
    memory.get_context_string.return_value = MEMORY_TEXT
    return memory


class TestClientSuppliedSystemMessage:
    """The autonomous pipeline's shape: messages[0] is the caller's own."""

    def _run(self, monkeypatch, **kwargs):
        memory = _fake_memory()
        result, msgs, system = _run_chat(monkeypatch, [
            ChatMessage(role="system", content="I am my own system prompt"),
            ChatMessage(role="user", content="hello"),
        ], memory=memory, **kwargs)
        return memory, result, msgs, system

    def test_retrieval_runs(self, monkeypatch):
        memory, result, _, _ = self._run(monkeypatch)
        assert result == {"id": "ok", "choices": []}, "request must still succeed"
        memory.get_context_string.assert_called_once()

    def test_the_block_actually_ships(self, monkeypatch):
        """The regression that mattered: retrieved, then thrown away."""
        _, _, msgs, _ = self._run(monkeypatch)
        assert MEMORY_TEXT in msgs[0].content, (
            "retrieved memories never reached the wire — this is DEV-488, the "
            "whole reason autonomous RAG did nothing")

    def test_the_callers_prompt_still_leads(self, monkeypatch):
        """Marker formats (<<<YAML>>>, <<<DESIGN>>>, <<<FILE:>>>) depend on the
        caller's prompt keeping its position."""
        _, _, msgs, _ = self._run(monkeypatch)
        assert msgs[0].content.startswith("I am my own system prompt")

    def test_still_exactly_one_system_message(self, monkeypatch):
        """Strict Jinja templates reject a second one."""
        _, _, msgs, _ = self._run(monkeypatch)
        assert sum(m.role == "system" for m in msgs) == 1

    def test_budget_guidance_survives(self, monkeypatch):
        """RAG was inserted next to it; neither may displace the other."""
        _, _, msgs, _ = self._run(monkeypatch)
        content = msgs[0].content
        assert "token" in content.lower(), "budget guidance went missing"
        assert content.index(MEMORY_TEXT) < content.rindex("token"), (
            "the budget line must stay last, nearest generation")

    def test_skip_memory_still_opts_out(self, monkeypatch):
        """skip_memory is now the single control; it must still work."""
        memory, _, msgs, _ = self._run(monkeypatch, skip_memory=True)
        memory.get_context_string.assert_not_called()
        assert MEMORY_TEXT not in msgs[0].content

    def test_nothing_retrieved_appends_nothing(self, monkeypatch):
        """An empty result must not leave a stray fence or blank block."""
        memory = mock.Mock()
        memory.get_context_string.return_value = ""
        _, msgs, _ = _run_chat(monkeypatch, [
            ChatMessage(role="system", content="I am my own system prompt"),
            ChatMessage(role="user", content="hello"),
        ], memory=memory)
        assert "MEMORY_CONTEXT" not in msgs[0].content


class TestAgentPromptRequests:
    """Positive control: the path that always worked must keep working."""

    def test_retrieval_runs_and_ships_on_the_agent_prompt(self, monkeypatch):
        memory = _fake_memory()
        result, msgs, system = _run_chat(monkeypatch, [
            ChatMessage(role="user", content="hello"),
        ], memory=memory)

        assert result == {"id": "ok", "choices": []}
        memory.get_context_string.assert_called_once()
        assert MEMORY_TEXT in system, (
            "with no client system message the block rides the agent prompt")

    def test_it_is_not_also_folded_into_a_user_message(self, monkeypatch):
        """The fold is only for the client_has_system branch."""
        _, msgs, _ = _run_chat(monkeypatch, [
            ChatMessage(role="user", content="hello"),
        ], memory=_fake_memory())
        assert all(MEMORY_TEXT not in m.content for m in msgs)
