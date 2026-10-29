"""Remove thinking and reasoning content from a completed model response.

The one implementation, shared by the server (which strips before it
responds) and the pipeline (which strips again, defensively, before parsing).
It lives in the pipeline package so the pipeline imports nothing from the
server: the server depends on the pipeline, never the reverse (DEV-837).
Standard library only.
"""
import re

# The tags are built rather than written out. Every model response this
# pipeline parses passes through strip_thinking, including one that emits this
# file, so a literal tag in it would be stripped out of the emitted source.
_OPEN = '<' + 'think>'
_CLOSE = '</' + 'think>'
_TAG_RE = re.compile(re.escape(_OPEN) + '|' + re.escape(_CLOSE))
_REACT_OPEN = '<' + 'REACT>'
_REACT_RE = re.compile(re.escape(_REACT_OPEN) + r'.*?' + re.escape('</' + 'REACT>') + r'\s*',
                       re.DOTALL)

# A tag with a quote or backtick on either side is text about the tag (the
# model quoting it in its reasoning, a string literal in emitted code), never
# a delimiter (DEV-936).
_QUOTES = frozenset('\'"`')


def _is_quoted(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ''
    after = text[end] if end < len(text) else ''
    return before in _QUOTES or after in _QUOTES


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _strip_think_blocks(text: str) -> str:
    """Models with --reasoning-format none still emit thinking in raw text,
    in three shapes:

      1. Full block:    open tag, reasoning, close tag, then the response
      2. Orphan close:  reasoning, close tag, response (Jinja consumed the open)
      3. Unclosed open: open tag, reasoning, end of text (max_tokens hit)

    Only unquoted tags delimit. A close tag that no open tag precedes ends an
    orphan block only when it is the first unquoted tag in the text; one after
    the response has begun is content and is kept.
    """
    tags = [(m.start(), m.end(), m.group(0) == _OPEN)
            for m in _TAG_RE.finditer(text)
            if not _is_quoted(text, m.start(), m.end())]
    if not tags:
        return text
    kept: list[str] = []
    pos = 0
    i = 0
    if not tags[0][2]:
        pos = _skip_space(text, tags[0][1])
        i = 1
    while i < len(tags):
        start, _end, is_open = tags[i]
        if not is_open:
            i += 1
            continue
        close = next((j for j in range(i + 1, len(tags)) if not tags[j][2]), None)
        kept.append(text[pos:start])
        if close is None:
            return ''.join(kept)
        pos = _skip_space(text, tags[close][1])
        i = close + 1
    kept.append(text[pos:])
    return ''.join(kept)


def strip_thinking(text: str) -> str:
    """Remove thinking/reasoning content from completed text.

    Short-circuit when no marker is present — the common case for non-
    reasoning models. Substring checks are far cheaper than a scan of a
    30K-token response.
    """
    has_think = _OPEN in text or _CLOSE in text
    has_react = _REACT_OPEN in text
    if not has_think and not has_react:
        return text
    if has_think:
        text = _strip_think_blocks(text)
    if has_react:
        text = _REACT_RE.sub('', text)  # Qwen3.5 reasoning blocks
    return text
