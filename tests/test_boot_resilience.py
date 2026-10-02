"""The server's optional services survive a bad boot (DEV-918).

Two outages this guards against, both seen in the journal:

- RAG was lost for a whole process lifetime when the network was late at boot:
  the embedding model load asked huggingface.co even with the model cached,
  memory init gave up after three quick attempts, and nothing retried.
- The Apple Deep Docs MCP failed to start after the OS upgrade stranded its
  venv. The child's stderr went to DEVNULL, so the journal never said why, and
  the lifespan dropped the service, so the route answered 503 until a restart
  even after the venv was fixed.
"""
import logging
import os
import sys
import time
from unittest import mock

from fastapi.testclient import TestClient

from coding_model_server import mcp_service
from coding_model_server import memory_service as ms


class _FakeSentenceTransformer:
    def __init__(self, calls, cached):
        self.calls, self.cached = calls, cached

    def __call__(self, name, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("local_files_only") and not self.cached:
            raise OSError("not in the local cache")
        return mock.Mock(name="model")


def test_embedding_model_loads_from_the_local_cache_without_the_network():
    calls = []
    ms._load_embedding_model(_FakeSentenceTransformer(calls, cached=True))
    assert calls == [{"device": "cpu", "local_files_only": True}]


def test_embedding_model_downloads_only_when_it_was_never_cached():
    calls = []
    ms._load_embedding_model(_FakeSentenceTransformer(calls, cached=False))
    assert calls == [{"device": "cpu", "local_files_only": True},
                     {"device": "cpu"}]


def _run_lifespan(memory_service_factory, deep_docs_starts):
    """Run the app lifespan with every heavy service stubbed."""
    import coding_model_server.server as srv
    from coding_model_server import runtime

    with mock.patch.object(srv, "MemoryService", memory_service_factory), \
            mock.patch.object(srv, "WebSearchService", return_value=mock.Mock()), \
            mock.patch.object(srv, "AppleDeepDocsService") as ADD, \
            mock.patch.object(srv, "MEMORY_INIT_RETRY_MIN_S", 0), \
            mock.patch.object(srv.gpu_sampler, "start"), \
            mock.patch.object(srv.gpu_sampler, "stop"), \
            mock.patch.object(runtime.llama_server_manager, "shutdown"):
        ADD.return_value.start.return_value = deep_docs_starts
        with TestClient(srv.app):
            deadline = time.monotonic() + 10
            while runtime.services.memory is None and time.monotonic() < deadline:
                time.sleep(0.02)
            return runtime.services.memory, runtime.services.apple_deep_docs, ADD


def test_memory_init_retries_after_a_failed_boot():
    fake_mem = mock.Mock(name="memory")
    factory = mock.Mock(side_effect=[RuntimeError("Cannot send a request, "
                                                  "as the client has been closed."),
                                     fake_mem])
    memory, _, _ = _run_lifespan(factory, deep_docs_starts=True)
    assert memory is fake_mem
    assert factory.call_count == 2


def test_deep_docs_service_is_kept_when_its_first_start_fails():
    """Kept, so the next request's start retry can recover it."""
    _, deep_docs, ADD = _run_lifespan(mock.Mock(return_value=mock.Mock()),
                                      deep_docs_starts=False)
    assert deep_docs is ADD.return_value


def test_a_failed_handshake_logs_the_childs_stderr(tmp_path, caplog):
    """A child that dies before the handshake says why, in the server log."""
    mcp = tmp_path / "appledeepdoc-mcp"
    (mcp / "venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, mcp / "venv" / "bin" / "python")
    (mcp / "main.py").write_text(
        "import sys\n"
        "sys.stderr.write(\"ModuleNotFoundError: No module named 'fastmcp'\\n\")\n"
        "sys.exit(1)\n")

    svc = mcp_service.AppleDeepDocsService(mcp_path=str(mcp))
    with caplog.at_level(logging.ERROR, logger=mcp_service.logger.name):
        assert svc.start() is False
    svc.stop()

    assert svc.stderr_log_path == str(tmp_path / "appledeepdoc-mcp.stderr.log")
    assert "No module named 'fastmcp'" in caplog.text
