"""Tests for server-side thinking-tag stripping.

The llama-server chat template leaks ``<think>`` reasoning into output; these
guard the two stripping paths the rest of the system relies on:
  * ``strip_thinking`` for complete (non-streamed) responses
  * ``ThinkingStripper`` for token-by-token streaming, where a tag may be split
    across chunk boundaries and a partial-tag tail is held back until ``flush``
"""
from coding_model_server.streaming import ThinkingStripper, strip_thinking


class TestStripThinking:
    def test_full_pair_removed(self):
        assert strip_thinking("<think>reasoning</think>answer") == "answer"

    def test_dangling_close_with_no_open(self):
        # Template ate the opening <think>; a bare </think> still delimits.
        assert strip_thinking("reasoning text</think>answer") == "answer"

    def test_multiple_pairs(self):
        assert strip_thinking("<think>a</think>X<think>b</think>Y") == "XY"

    def test_plain_text_untouched(self):
        assert strip_thinking("just a normal answer") == "just a normal answer"

    def test_empty_string(self):
        assert strip_thinking("") == ""

    def test_leading_whitespace_stripped(self):
        assert strip_thinking("<think>x</think>\n  answer") == "answer"


class TestThinkingStripper:
    @staticmethod
    def _drain(chunks):
        # feed() holds back a partial-tag tail; flush() releases the remainder,
        # so a faithful end-to-end result needs both.
        s = ThinkingStripper()
        return "".join(s.feed(c) for c in chunks) + s.flush()

    def test_tag_split_across_chunks(self):
        # The defining case: <think> and </think> straddle chunk boundaries.
        assert self._drain(["<th", "ink>secret</thi", "nk>visible"]) == "visible"

    def test_no_thinking_passes_through(self):
        assert self._drain(["no think here ", "just text"]) == "no think here just text"

    def test_single_chunk_with_pair(self):
        assert self._drain(["<think>x</think>y"]) == "y"

    def test_thinking_only_yields_nothing(self):
        assert self._drain(["<think>all reasoning</think>"]) == ""

    def test_flush_releases_buffered_text_when_no_close_tag(self):
        # A non-thinking model never emits </think>; feed() buffers while it
        # waits, and flush() must release that text rather than drop it.
        s = ThinkingStripper()
        s.feed("plain answer")
        assert s.flush() == "plain answer"
