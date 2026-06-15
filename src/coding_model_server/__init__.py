"""Coding Model multi-agent server package.

Hosts the FastAPI inference server (``server``), the autonomous
orchestrator daemon (``orchestrator_daemon``), and the loose modules
they share (``config``, ``streaming``, ``tool_handlers``,
``code_chunker``, ``external_judges``, ``llama_server``,
``memory_service``, ``metrics``, ``mcp_service``,
``web_search_service``).

Sibling packages ``coding_model_client`` and ``coding_model_autonomous`` live under the
same ``src/`` root and import from this package by absolute name (e.g.
``from coding_model_server import external_judges``).
"""
