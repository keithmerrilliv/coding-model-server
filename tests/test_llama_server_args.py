"""Tests for LlamaServerManager._build_server_args and start().

The argv builder was extracted from start() precisely so it could be tested
without spawning a subprocess. These pin the flag generation: base flags,
cache-type mapping, the cpu_moe toggle, the optional speculative-decode draft
block, and the server_extra_args default.

test_start_executes_post_popen_body is a regression guard: the argv extraction
left an orphaned `model_path` reference in start() (NameError at runtime, missed
by the isolated builder tests because they never ran start()). This drives
start() through its post-Popen body with the subprocess + health poll mocked.
"""
import os
from unittest import mock

import pytest
import requests as http_requests

from coding_model_server.llama_server import LlamaServerManager


@pytest.fixture
def mgr():
    # No side effects in __init__; safe to construct directly.
    return LlamaServerManager()


def _pairs(cmd):
    """Map '-flag value' pairs from an argv list for easy assertions.

    Value-less flags (e.g. --cpu-moe, --mmap) map to True.
    """
    out = {}
    i = 1  # skip binary
    while i < len(cmd):
        tok = cmd[i]
        if tok.startswith("-"):
            if i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
                out[tok] = cmd[i + 1]
                i += 2
            else:
                out[tok] = True
                i += 1
        else:
            i += 1
    return out


def test_base_flags_present(mgr):
    cmd = mgr._build_server_args("/bin/llama-server", {"path": "/models/m.gguf"})
    assert cmd[0] == "/bin/llama-server"
    p = _pairs(cmd)
    assert p["-m"] == "/models/m.gguf"
    assert p["-c"] == "32768"          # default n_ctx
    assert p["-fa"] == "auto"
    assert p["--cache-reuse"] == "256"
    assert p["--mmap"] is True


def test_numeric_config_flows_through(mgr):
    cmd = mgr._build_server_args("/b", {
        "path": "/m.gguf", "n_gpu_layers": 48, "n_ctx": 262144,
        "n_batch": 4096, "n_ubatch": 2048,
    })
    p = _pairs(cmd)
    assert p["-ngl"] == "48"
    assert p["-c"] == "262144"
    assert p["-b"] == "4096"
    assert p["-ub"] == "2048"


def test_cache_type_integers_map_to_names(mgr):
    # 8 -> q8_0, 2 -> q4_0 per _CACHE_TYPE_NAMES
    cmd = mgr._build_server_args("/b", {"path": "/m.gguf", "type_k": 2, "type_v": 8})
    p = _pairs(cmd)
    assert p["--cache-type-k"] == "q4_0"
    assert p["--cache-type-v"] == "q8_0"


def test_cache_type_defaults_to_q8_0(mgr):
    cmd = mgr._build_server_args("/b", {"path": "/m.gguf"})
    p = _pairs(cmd)
    assert p["--cache-type-k"] == "q8_0"
    assert p["--cache-type-v"] == "q8_0"


def test_cpu_moe_toggle(mgr):
    assert "--cpu-moe" in mgr._build_server_args("/b", {"path": "/m.gguf", "cpu_moe": True})
    assert "--cpu-moe" not in mgr._build_server_args("/b", {"path": "/m.gguf"})


def test_n_cpu_moe_emits_flag_and_overrides_cpu_moe(mgr):
    # n_cpu_moe -> --n-cpu-moe N (partial expert offload to GPU)
    args = mgr._build_server_args("/b", {"path": "/m.gguf", "n_cpu_moe": 26})
    assert "--n-cpu-moe" in args
    assert args[args.index("--n-cpu-moe") + 1] == "26"
    # n_cpu_moe takes precedence over cpu_moe (no bare --cpu-moe emitted)
    both = mgr._build_server_args("/b", {"path": "/m.gguf", "cpu_moe": True, "n_cpu_moe": 26})
    assert "--n-cpu-moe" in both and "--cpu-moe" not in both
    # absent -> neither offload flag
    none = mgr._build_server_args("/b", {"path": "/m.gguf"})
    assert "--n-cpu-moe" not in none and "--cpu-moe" not in none


def test_no_draft_flags_when_absent(mgr):
    cmd = mgr._build_server_args("/b", {"path": "/m.gguf"})
    assert "-md" not in cmd
    assert "-devd" not in cmd


def test_draft_block_emitted(mgr):
    cmd = mgr._build_server_args("/b", {
        "path": "/m.gguf",
        "draft": {"path": "/draft.gguf", "n_gpu_layers": 0, "draft_max": 4},
    })
    p = _pairs(cmd)
    assert p["-md"] == "/draft.gguf"
    assert p["-ngld"] == "0"
    assert p["--draft-max"] == "4"
    assert p["-devd"] == "none"        # default device when unspecified


def test_draft_cmoed_when_cpu_moe(mgr):
    cmd = mgr._build_server_args("/b", {
        "path": "/m.gguf",
        "draft": {"path": "/d.gguf", "cpu_moe": True},
    })
    assert "-cmoed" in cmd


def test_extra_args_default_chat_template(mgr):
    cmd = mgr._build_server_args("/b", {"path": "/m.gguf"})
    assert "--chat-template" in cmd
    assert cmd[cmd.index("--chat-template") + 1] == "chatml"


def test_extra_args_override(mgr):
    cmd = mgr._build_server_args("/b", {
        "path": "/m.gguf", "server_extra_args": ["--jinja", "--swa-full"],
    })
    assert "--jinja" in cmd and "--swa-full" in cmd
    assert "--chat-template" not in cmd  # override replaces the default


def test_start_executes_post_popen_body(mgr, monkeypatch, tmp_path):
    """Drive start() through its full post-Popen body without a real subprocess.

    Regression guard: the _build_server_args extraction orphaned a `model_path`
    name in start() (NameError), which the isolated builder tests above could not
    catch because they never executed start(). Here we make the binary check
    pass, stub Popen + the stdout drain + the /health poll, and assert start()
    runs clean and records current_model_path from the config.
    """
    model_config = {"path": "/models/m.gguf", "n_gpu_layers": 1, "n_ctx": 4096}

    # 1) binary existence check -> always true
    monkeypatch.setattr(os.path, "isfile", lambda p: True)

    # 2) fake process: alive (poll None), drainable stdout
    fake_proc = mock.Mock()
    fake_proc.poll.return_value = None
    fake_proc.stdout.readline.side_effect = lambda: b""  # drain loop exits immediately
    monkeypatch.setattr("coding_model_server.llama_server.subprocess.Popen",
                        lambda *a, **k: fake_proc)

    # 3) /health returns 200 on first poll, then /props is read for the chat
    #    template (both go through the shared session, so route on the URL).
    healthy = mock.Mock(status_code=200)
    props = mock.Mock(status_code=200)
    props.json.return_value = {
        "chat_template": "{%- for m in messages -%}{{ m.content }}{%- endfor -%}",
        "chat_template_caps": {"supports_preserve_reasoning": False},
    }
    monkeypatch.setattr(mgr._session, "get",
                        lambda url, *a, **k: props if "/props" in url else healthy)

    # 4) don't spin up the real idle watchdog thread
    monkeypatch.setattr(mgr, "_start_watchdog", lambda: None)

    # 5) DEV-157 pre-flight is unit-tested on its own; here the stubbed
    #    session answers 200 for every URL, which the orphan check would
    #    (correctly) treat as a foreign server squatting on the port.
    monkeypatch.setattr(mgr, "_reap_orphan_llama_server", lambda: None)
    monkeypatch.setattr(LlamaServerManager, "_PIDFILE", tmp_path / "llama.pid")

    mgr.start(model_config)  # would raise NameError before the fix

    assert mgr.current_model_path == "/models/m.gguf"
    assert mgr.process is fake_proc
    # A template with no <think> means nothing to strip -> stream incrementally.
    assert mgr.current_expects_thinking is False


class TestProbeExpectsThinking:
    """Which models proxy_stream is allowed to stream token-by-token.

    Getting this wrong in the permissive direction leaks reasoning to a client
    that EXECUTES <<<TOOL>>> markers, so every unclear case must answer True.
    """

    def _mgr_with_props(self, mgr, monkeypatch, payload):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = payload
        monkeypatch.setattr(mgr._session, "get", lambda *a, **k: resp)
        return mgr

    def test_plain_chatml_template_streams(self, mgr, monkeypatch):
        # Qwen3-Coder-*-Instruct: no <think>, no reasoning caps.
        self._mgr_with_props(mgr, monkeypatch, {
            "chat_template": "<|im_start|>assistant\n",
            "chat_template_caps": {"supports_preserve_reasoning": False},
        })
        assert mgr._probe_expects_thinking() is False

    def test_template_opening_a_think_block_buffers(self, mgr, monkeypatch):
        self._mgr_with_props(mgr, monkeypatch, {
            "chat_template": "<|im_start|>assistant\n<think>\n",
            "chat_template_caps": {"supports_preserve_reasoning": False},
        })
        assert mgr._probe_expects_thinking() is True

    def test_reasoning_capability_alone_buffers(self, mgr, monkeypatch):
        # No literal <think>, but llama.cpp says the template handles reasoning.
        # Either signal is enough to withhold.
        self._mgr_with_props(mgr, monkeypatch, {
            "chat_template": "<|im_start|>assistant\n",
            "chat_template_caps": {"supports_preserve_reasoning": True},
        })
        assert mgr._probe_expects_thinking() is True

    def test_unreachable_props_buffers(self, mgr, monkeypatch):
        def boom(*a, **k):
            raise http_requests.ConnectionError("refused")
        monkeypatch.setattr(mgr._session, "get", boom)
        assert mgr._probe_expects_thinking() is True

    def test_malformed_props_buffers_instead_of_raising(self, mgr, monkeypatch):
        # A shape we don't recognise must not take the model load down.
        self._mgr_with_props(mgr, monkeypatch, ["not", "an", "object"])
        assert mgr._probe_expects_thinking() is True
