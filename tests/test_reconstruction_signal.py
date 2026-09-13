"""DEV-536, folded into DEV-630: the runner's overwrite signal is consumed.

`mac_runner/workspace.py` records, per overwritten file, the before/after line
counts plus `suspected_reconstruction` (new < old * RUNNER_OVERWRITE_SHRINK_RATIO)
and `server.py` returns it on `RunTestsResponse.overwrites`. Nothing on the
orchestrator side read it: `_run_mac_runner_tests` took `passed` and `output`
off the response and dropped the rest, so the detector helped only someone
reading the Mac's log by hand.

Since DEV-492's read path removed the CAUSE, this is a regression detector —
the thing that says the read path has silently stopped working. It now heads
the output (first, not last, as the runner does with its own integration
warnings), which is what the reviewer, the retry feedback and the human at the
gate all read.
"""
from unittest import mock

from coding_model_autonomous import test_runner as tr

SHRUNK = {"path": "Sources/S.swift", "old_lines": 163, "new_lines": 45,
          "old_chars": 5000, "new_chars": 1200, "suspected_reconstruction": True}
EDITED = {"path": "Sources/T.swift", "old_lines": 50, "new_lines": 48,
          "old_chars": 1500, "new_chars": 1450, "suspected_reconstruction": False}


def _dispatch(body, *, protected=None):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return body

    with mock.patch.object(tr, "MAC_RUNNER_API_KEY", "k"), \
         mock.patch.object(tr, "MAC_RUNNER_URL", "http://runner"), \
         mock.patch.object(tr, "_collect_patch_files",
                           return_value=([{"path": "Sources/S.swift", "content": "x"},
                                          {"path": "Sources/P.swift", "content": "x"}],
                                         None)), \
         mock.patch.object(tr._SESSION, "post", return_value=_Resp()):
        return tr._run_mac_runner_tests(
            spec_dir=mock.MagicMock(name="spec_dir"),
            framework="swift_test", timeout=60,
            repo="centipede", base_ref="main",
            protected_paths=protected or [],
        )


def test_a_suspected_reconstruction_heads_the_output():
    passed, output = _dispatch({"passed": False, "output": "Executed 2 tests",
                                "overwrites": [SHRUNK, EDITED]})
    assert passed is False                                   # unchanged
    assert output.startswith(tr.RECONSTRUCTION_MARKER)       # first, not last
    assert "Sources/S.swift: 163 -> 45 lines" in output
    assert "never read, re-emitted from imagination" in output
    assert output.rstrip().endswith("Executed 2 tests")      # the real output kept


def test_an_ordinary_overwrite_is_not_flagged():
    _, output = _dispatch({"passed": True, "output": "Executed 2 tests",
                           "overwrites": [EDITED]})
    assert tr.RECONSTRUCTION_MARKER not in output
    assert "Sources/T.swift" not in output
    assert output == "Executed 2 tests"


def test_a_response_without_the_field_is_untouched():
    """Older runners, or the local sandbox path, send no `overwrites`."""
    _, output = _dispatch({"passed": True, "output": "Executed 2 tests"})
    assert output == "Executed 2 tests"


def test_it_survives_a_malformed_entry():
    _, output = _dispatch({"passed": True, "output": "ok",
                           "overwrites": ["not a dict", None, SHRUNK]})
    assert output.startswith(tr.RECONSTRUCTION_MARKER)
    assert "Sources/S.swift" in output


def test_the_protected_paths_block_stays_outermost():
    """Both prefixes can apply. The daemon sniffs the marker in the first 2000
    chars of output, so it must survive being wrapped by the protected block."""
    _, output = _dispatch({"passed": False, "output": "Executed 2 tests",
                           "overwrites": [SHRUNK]},
                          protected=["Sources/P.swift"])
    assert output.startswith("[protected paths]")
    assert tr.RECONSTRUCTION_MARKER in output[:2000]
