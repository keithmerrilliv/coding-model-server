"""Run the chat client with `python -m coding_model_client`.

Mirrors the historical `client.py` shim that was deleted when the loose
top-level modules moved into ``src/``. The ``coding-model-client`` console
script declared in ``pyproject.toml`` is the user-facing equivalent.
"""
import sys

from coding_model_client.main import main

sys.exit(main())
