"""Run the chat client with `python -m qwen_client`.

Mirrors the historical `client.py` shim that was deleted when the loose
top-level modules moved into ``src/``. The ``qwen-client`` console
script declared in ``pyproject.toml`` is the user-facing equivalent.
"""
import sys

from qwen_client.main import main

sys.exit(main())
