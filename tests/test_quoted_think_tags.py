"""A quoted think tag is text, not a delimiter (DEV-936).

strip_thinking runs on every completed response, the server's and the
pipeline's. It used to split on the first close tag anywhere and remove every
open/close pair anywhere, so a reasoning block that quoted the close tag leaked
the rest of the reasoning into the answer, and emitted code that held a tag
pair in a string literal was silently rewritten. The tags and the FILE markers
are built here, never written out: a model that emits this file passes it
through strip_thinking and the FILE-marker parser, and a literal of either would
be stripped or split out of the emitted source (spec_74269dcd).
"""
from coding_model_autonomous.thinking import strip_thinking

OPEN = "<" + "think>"
CLOSE = "</" + "think>"
REACT = "<" + "REACT>"
REACT_END = "</" + "REACT>"
FILE_A = "<<<" + "FILE: a.py>>>"
FILE_TEST = "<<<" + "FILE: tests/test_a.py>>>"
END_FILE = "<<<" + "END_FILE>>>"


def test_a_full_block_is_still_removed():
    assert strip_thinking(f"{OPEN}plan{CLOSE}\n\nanswer") == "answer"


def test_an_orphan_close_still_ends_the_reasoning():
    assert strip_thinking(f"I'll write it.{CLOSE}\n\nanswer") == "answer"


def test_unclosed_reasoning_is_still_dropped():
    assert strip_thinking(f"answer{OPEN}truncated reasoning") == "answer"


def test_reasoning_that_quotes_the_close_tag_is_removed_whole():
    """The DEV-936 shape: the model quotes the tag while reasoning about it.
    Quoted twice, because the old two-pass strip happened to survive one."""
    text = (f"{OPEN}The parser splits on `{CLOSE}`, and a second `{CLOSE}`\n"
            f"would end it again, so I must not end early.\n"
            f"{CLOSE}\n\nVERDICT: PASS")
    assert strip_thinking(text) == "VERDICT: PASS"


def test_a_tag_in_a_string_literal_survives_with_no_reasoning():
    code = f"{FILE_A}\nCLOSE = '{CLOSE}'\n{END_FILE}"
    assert strip_thinking(code) == code


def test_a_quoted_pair_in_emitted_code_is_kept_after_real_reasoning():
    answer = (f"{FILE_TEST}\n"
              f"assert strip(\"{OPEN}r{CLOSE}ok\") == \"ok\"\n{END_FILE}")
    assert strip_thinking(f"{OPEN}plan{CLOSE}\n{answer}") == answer


def test_an_unquoted_close_after_the_answer_began_is_content():
    text = f"{OPEN}plan{CLOSE}\nanswer mentions {CLOSE} once"
    assert strip_thinking(text) == f"answer mentions {CLOSE} once"


def test_text_without_tags_is_returned_as_is():
    text = "plain answer"
    assert strip_thinking(text) is text


def test_a_react_block_is_still_removed():
    assert strip_thinking(f"{REACT}reasoning{REACT_END}\nanswer") == "answer"
