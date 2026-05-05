"""Qwen multi-agent server package.

Hosts the FastAPI inference server (``server``), the autonomous
orchestrator daemon (``orchestrator_daemon``), and the loose modules
they share (``config``, ``streaming``, ``tool_handlers``,
``code_chunker``, ``external_judges``, ``llama_server``,
``memory_service``, ``metrics``, ``server_manager``,
``web_search_service``).

Sibling packages ``qwen_client`` and ``qwen_autonomous`` live under the
same ``src/`` root and import from this package by absolute name (e.g.
``from qwen_server import external_judges``).
"""
