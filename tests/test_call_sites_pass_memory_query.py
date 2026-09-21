"""DEV-497: every agent call site in the daemon names its retrieval query.

DEV-489 gave the architect and the per-file implementer an explicit
`memory_query`; the single-call implementer, the manifest call, synthesis,
repair and both reviewers did not get one, so the moment a role opted in
(DEV-510, run 29) retrieval keyed on the last user message — the DEV-546
conditions block plus the spec preamble, truncated at 256 tokens. This scan
keeps a future call site from silently inheriting that fallback: a
`call_agent(` in the daemon must pass `memory_query=` and `language=`, or
carry a `# no-memory-query:` comment saying why not.
"""
import re
from pathlib import Path

DAEMON = Path(__file__).resolve().parents[1] / "src" / "coding_model_server" / "orchestrator_daemon.py"
_CALL = re.compile(r"\bcall_agent\(")


def _call_texts(source: str):
    """Each call_agent(...) invocation's full argument text, with its line."""
    out = []
    for m in _CALL.finditer(source):
        depth, i = 0, m.end() - 1
        while i < len(source):
            c = source[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        text = source[m.start():i + 1]
        line = source.count("\n", 0, m.start()) + 1
        # a trailing exemption comment on the same line as the call
        eol = source.find("\n", m.start())
        exempt = "no-memory-query:" in source[m.start():eol]
        out.append((line, text, exempt))
    return out


def test_every_daemon_call_agent_site_names_its_query_and_language():
    source = DAEMON.read_text()
    sites = _call_texts(source)
    assert len(sites) >= 8, "the scan found fewer call sites than the daemon has"
    missing = [line for line, text, exempt in sites
               if not exempt and ("memory_query=" not in text or "language=" not in text)]
    assert not missing, f"call_agent sites without memory_query/language at lines {missing}"


def test_single_call_implementer_uses_the_spec_title():
    source = DAEMON.read_text()
    sites = _call_texts(source)
    impl = [text for _, text, _ in sites if text.lstrip().startswith('call_agent("implementer", messages, agent=chosen_agent')]
    assert impl and "spec_memory_query(spec_md)" in impl[0]
