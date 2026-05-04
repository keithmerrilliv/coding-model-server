"""Thinking-tag stripping and OpenAI-compatible response/chunk builders.

Extracted from server.py for organization. No external dependencies beyond
the standard library.
"""
import re
import time
import uuid
from typing import Any, Dict, List, Optional


# ============================================================================
# Thinking Tag Stripping
# ============================================================================

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


class ThinkingStripper:
    """Streaming state machine that suppresses thinking content.

    Handles two patterns:
        1. <think>reasoning...</think>response  (full block)
        2. reasoning...</think>response          (orphan — Jinja consumed <think>)

    Buffers all tokens until </think> is found, then emits everything after it.
    Thinking content can itself contain tool markers (<<<WRITE_FILE>>> etc.)
    as the model reasons about tools, so we cannot use those as shortcuts.

    If no </think> appears within MAX_BUFFER chars, assumes no thinking block
    and flushes the buffer as real content.
    """
    BUFFERING = 0
    PASSTHROUGH = 1

    MAX_BUFFER = 32768  # chars — Nemotron generates 8K+ thinking blocks

    # The closing tag we're looking for; cached to avoid re-computing length.
    _CLOSE_TAG = '</think>'
    _CLOSE_LEN = len(_CLOSE_TAG)

    def __init__(self):
        self.state = self.BUFFERING
        self.buffer = ''
        # How far we've already scanned for the close tag. Without this,
        # find('</think>') restarts at index 0 every chunk, making the
        # accumulator O(n²) over buffer length.
        self._search_start = 0

    def feed(self, token: str) -> str:
        """Feed a token, return text to emit (empty string = suppress)."""
        if self.state == self.PASSTHROUGH:
            return token

        prev_len = len(self.buffer)
        self.buffer += token

        # Scan only the new tail (minus tag-length-1 to catch tag boundaries
        # that span the previous chunk + this one).
        scan_from = max(0, prev_len - (self._CLOSE_LEN - 1))
        close_idx = self.buffer.find(self._CLOSE_TAG, scan_from)
        if close_idx != -1:
            after = self.buffer[close_idx + self._CLOSE_LEN:]
            self.state = self.PASSTHROUGH
            self.buffer = ''
            return after.lstrip()

        # Safety valve: if buffer grows too large without </think>,
        # this response has no thinking block — flush as real content
        if len(self.buffer) >= self.MAX_BUFFER:
            self.state = self.PASSTHROUGH
            result = self.buffer
            self.buffer = ''
            return result

        return ''

    def flush(self) -> str:
        """Flush remaining buffer at end of stream.

        If still in BUFFERING state, no </think> was ever seen.  This means
        either: (a) the model doesn't use thinking tags (common — e.g. chatml
        models), so the buffer is the entire valid response, or (b) an orphaned
        <think> block was never closed (rare).  We return the buffer in both
        cases — discarding it would drop valid tool calls for non-thinking models.
        """
        if self.buffer:
            result = self.buffer
            self.buffer = ''
            self.state = self.PASSTHROUGH
            return result
        return ''


# ============================================================================
# Response Builders
# ============================================================================

def build_completion_response(model_id: str, text: str, usage: Dict[str, int],
                              finish_reason: str = "stop",
                              tool_calls: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build OpenAI-compatible completion response"""
    message: Dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason
        }],
        "usage": {
            "prompt_tokens": usage['prompt_tokens'],
            "completion_tokens": usage['completion_tokens'],
            "total_tokens": usage['total_tokens']
        }
    }


def build_stream_chunk(completion_id: str, model_id: str, content: Optional[str] = None,
                       finish: bool = False, finish_reason: Optional[str] = None,
                       tool_calls: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build OpenAI-compatible streaming chunk.

    `tool_calls` is the raw `delta.tool_calls` array from llama-server (list of
    {index, id?, type?, function:{name?, arguments?}} dicts) — passed through
    untouched so the client can reassemble parallel calls by index.
    """
    if finish and not finish_reason:
        finish_reason = "stop"
    delta: Dict[str, Any] = {}
    if content:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason if finish else None
        }]
    }
