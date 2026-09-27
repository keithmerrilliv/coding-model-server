"""Coding Model multi-agent server package.

Hosts the FastAPI inference server (``server``, ``routes/``,
``llama_server``, ``streaming``, ``memory_service``, ``metrics``,
``mcp_service``, ``web_search_service``, ``code_chunker``), the agent roster
(``config``), and the autonomous orchestrator daemon
(``orchestrator_daemon``).

Import direction: this package imports ``coding_model_autonomous`` (the daemon
drives the pipeline); the pipeline imports nothing from here (DEV-837).

Two modules live here but serve only the interactive client:
``tool_handlers`` (imported and run BY THE CLIENT, on the operator's machine)
and ``external_judges`` (the client's /review and scripts/eval_agents.py).
Neither the server nor the daemon imports them.
"""
