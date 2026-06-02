"""FastAPI route modules, each an APIRouter included by qwen_server.server.

Split out of the former monolithic server.py:
  - meta:       /, /health, /v1/models
  - memory:     /v1/memory*, /v1/tools/* (memory + web + Apple docs)
  - autonomous: /v1/autonomous/* (spec store + review gates)
  - admin:      /v1/admin/* (metrics, gpu, active model)
  - chat:       /v1/chat/completions (+ prompt-assembly helpers)
"""
