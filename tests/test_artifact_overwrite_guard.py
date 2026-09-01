import logging
from pathlib import Path

import pytest

from coding_model_autonomous.executor import _write_artifact


class TestArtifactOverwriteGuard:
    def test_a1_collision_refused(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.WARNING)
        p = "P.txt"
        impl_content = "impl code\n"
        rev_content = "rev code\n"

        # Implementer writes P
        result1 = _write_artifact(tmp_path, p, impl_content)
        assert result1 == tmp_path / p
        assert (tmp_path / p).read_text() == impl_content

        # Reviewer tries to write same P in same attempt
        prior_writes = [(p, "implementer")]
        result2 = _write_artifact(
            tmp_path, p, rev_content,
            prior_writes=prior_writes, role="reviewer",
        )
        assert result2 is None
        assert (tmp_path / p).read_text() == impl_content
        assert "collision_refused" in caplog.text
        assert p in caplog.text
        assert "implementer" in caplog.text
        assert "reviewer" in caplog.text

        # Reviewer write to fresh Q succeeds
        q = "Q.txt"
        result3 = _write_artifact(
            tmp_path, q, "q content\n",
            prior_writes=prior_writes, role="reviewer",
        )
        assert result3 == tmp_path / q
        assert (tmp_path / q).read_text() == "q content\n"

    def test_a2_emptying_refused(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.WARNING)
        z = "Z.py"
        code_content = "def foo():\n    pass\n"
        comment_only = "# just a comment\n"

        # Impl writes Z with one def
        result1 = _write_artifact(tmp_path, z, code_content)
        assert result1 == tmp_path / z
        assert (tmp_path / z).read_text() == code_content

        # SAME role rewrites Z with comment-only -> refused
        prior_writes = [(z, "implementer")]
        result2 = _write_artifact(
            tmp_path, z, comment_only,
            prior_writes=prior_writes, role="implementer",
        )
        assert result2 is None
        assert (tmp_path / z).read_text() == code_content
        assert "emptying_refused" in caplog.text
        assert z in caplog.text
        assert "implementer" in caplog.text

        # Same-role rewrite of Z with different real code succeeds
        new_code = "def bar():\n    return 42\n"
        result3 = _write_artifact(
            tmp_path, z, new_code,
            prior_writes=prior_writes, role="implementer",
        )
        assert result3 == tmp_path / z
        assert (tmp_path / z).read_text() == new_code

    def test_a3_ordinary_flow_no_refusals(self, tmp_path: Path):
        r = "R.py"
        content = "def hello():\n    print('hi')\n"

        # Ordinary single-producer flow (no kwargs)
        result = _write_artifact(tmp_path, r, content)
        assert result == tmp_path / r
        assert (tmp_path / r).read_text() == content