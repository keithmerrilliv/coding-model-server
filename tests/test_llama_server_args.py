"""Tests for LlamaServerManager._build_server_args.

The argv builder was extracted from start() precisely so it could be tested
without spawning a subprocess. These pin the flag generation: base flags,
cache-type mapping, the cpu_moe toggle, the optional speculative-decode draft
block, and the server_extra_args default.
"""
import pytest

from qwen_server.llama_server import LlamaServerManager


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
