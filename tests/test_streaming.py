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

    def test_reasoning_never_reaches_the_client(self):
        # The safety property. Reasoning may contain tool markers while the model
        # merely considers a call, and the client EXECUTES markers -- so a leak
        # here runs a shell command the model never committed to.
        out = self._drain(["thinking about <<<SHELL>>>rm -rf /", "</think>", "done"])
        assert out == "done"
        assert "SHELL" not in out


class TestThinkingStripperIncremental:
    """expect_thinking=False: models whose template never opens <think>.

    Buffering these stalled the entire response into one chunk at flush() --
    78s of silence for a 500-token reply on the 480B at 6.4 tok/s.
    """

    def test_streams_first_token_immediately(self):
        s = ThinkingStripper(expect_thinking=False)
        assert s.feed("Hello") == "Hello"
        assert s.feed(" world") == " world"

    def test_tool_marker_is_not_mistaken_for_a_tag(self):
        # <<<WRITE_FILE>>> opens with '<', a viable prefix of '<think>'. The
        # stripper may hold it for one token, but must not swallow it.
        s = ThinkingStripper(expect_thinking=False)
        out = "".join(s.feed(c) for c in ["<", "<<WRITE_FILE>>>x"]) + s.flush()
        assert out == "<<<WRITE_FILE>>>x"

    def test_holds_back_at_most_a_partial_open_tag(self):
        # '<' alone is ambiguous, so it is held -- but only until the next token.
        s = ThinkingStripper(expect_thinking=False)
        assert s.feed("<") == ""
        assert s.feed("p>hi") == "<p>hi"

    def test_still_strips_a_think_block_if_one_shows_up(self):
        # The template says this model doesn't reason, but nothing stops it from
        # emitting the tag anyway. Rule it out rather than assume.
        s = ThinkingStripper(expect_thinking=False)
        out = "".join(s.feed(c) for c in ["<think>", "secret", "</think>", "answer"])
        assert out + s.flush() == "answer"

    def test_flush_releases_an_undecided_tail(self):
        # Stream ends while still deciding: the held text must not vanish.
        s = ThinkingStripper(expect_thinking=False)
        assert s.feed("<") == ""
        assert s.flush() == "<"


class TestThinkingStripperTruncation:
    """DEV-119: an unterminated think buffer must never leak on truncation.

    A buffer with no </think> is ambiguous — hybrid answer vs cut-off
    reasoning — and only the upstream finish_reason can discriminate. The
    caller passes that verdict as flush(truncated=...).
    """

    def test_truncated_reasoning_is_suppressed(self):
        # The safety property: reasoning cut off by max_tokens may contain
        # tool markers the client executes for real.
        s = ThinkingStripper()
        s.feed("I should run <<<REMOTE_EXEC>>>rm -rf /tmp/x to clean up")
        assert s.flush(truncated=True) == ""
        assert s.dropped_chars > 0

    def test_clean_stop_still_emits_a_hybrid_answer(self):
        # Qwen3.6-style hybrid in non-thinking mode: template says thinking,
        # output has no tags, finish is a clean stop. Dropping this would eat
        # every response of the supervisor/dense_architect agents.
        s = ThinkingStripper()
        s.feed("a real answer with no tags")
        assert s.flush(truncated=False) == "a real answer with no tags"

    def test_clean_stop_drops_a_literal_unclosed_think_block(self):
        # A literal <think> with no close is confirmed reasoning even on a
        # clean stop — same verdict the sync path's regexes give it.
        s = ThinkingStripper(expect_thinking=False)
        assert s.feed("<think>") == ""
        assert s.feed("secret <<<SHELL>>>evil") == ""
        assert s.flush(truncated=False) == ""

    def test_overflow_never_flushes_midstream(self):
        # The old 32K "safety valve" streamed the buffer as content — the
        # exact leak. Now the cap only trims memory, emitting nothing.
        s = ThinkingStripper()
        big = "x" * (ThinkingStripper.MAX_BUFFER + 10)
        assert s.feed(big) == ""
        assert s.flush(truncated=True) == ""

    def test_late_close_after_overflow_recovers_the_answer(self):
        # The trimmed buffer keeps a partial-tag tail, so a </think> arriving
        # after the cap still switches to passthrough and the answer survives.
        s = ThinkingStripper()
        assert s.feed("r" * (ThinkingStripper.MAX_BUFFER + 5)) == ""
        assert s.feed("</think>the answer") == "the answer"

    def test_close_tag_split_across_the_trim_boundary(self):
        s = ThinkingStripper()
        assert s.feed("r" * ThinkingStripper.MAX_BUFFER + "</thi") == ""
        assert s.feed("nk>done") == "done"
