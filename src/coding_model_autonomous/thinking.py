"""Remove thinking and reasoning content from a completed model response.

The one implementation, shared by the server (which strips before it
responds) and the pipeline (which strips again, defensively, before parsing).
It lives in the pipeline package so the pipeline imports nothing from the
server: the server depends on the pipeline, never the reverse (DEV-837).
Standard library only.
"""
import re

# Models with --reasoning-format none still emit thinking content in raw text.
# Three patterns observed:
#   1. Full block:    <think>reasoning...</think>actual response
#   2. Orphan close:  reasoning...</think>actual response  (Jinja consumed <think>)
#   3. Unclosed open: <think>reasoning...  (truncated by max_tokens, no </think>)
_THINK_FULL_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL)
_THINK_ORPHAN_RE = re.compile(r'^.*?</think>\s*', re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r'<think>(?:(?!</think>).)*$', re.DOTALL)
_REACT_RE = re.compile(r'<REACT>.*?</REACT>\s*', re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove thinking/reasoning content from completed text.

    Short-circuit when no marker is present — the common case for non-
    reasoning models. Substring checks are far cheaper than four DOTALL
    regex passes over a 30K-token response.
    """
    has_think = '<think>' in text or '</think>' in text
    has_react = '<REACT>' in text
    if not has_think and not has_react:
        return text
    if has_think:
        text = _THINK_FULL_RE.sub('', text)
        text = _THINK_ORPHAN_RE.sub('', text)
        text = _THINK_UNCLOSED_RE.sub('', text)  # Truncated thinking (hit max_tokens)
    if has_react:
        text = _REACT_RE.sub('', text)  # Qwen3.5 reasoning blocks
    return text
