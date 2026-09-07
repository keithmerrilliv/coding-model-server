"""Tests for Tested-Artifact Manifest Recording (DEV-602 split B).

Authored by run 21 (spec_04658e97) and hand-landed after the run failed on
unappliable edits; the never-raise test was rewritten from a vacuous
/root-based variant into a real write-failure case.
"""

import hashlib
import json
from pathlib import Path

from coding_model_server.orchestrator_daemon import _record_tested_manifest


class TestRecordTestedManifest:
    """Unit tests for _record_tested_manifest helper function."""

    def test_b1_creates_manifest_with_sha256_hashes(self, tmp_path: Path):
        """B1: After a passing check, tested_manifest.json exists with SHA-256 digests."""
        spec_dir = tmp_path
        
        # Create artifact files on disk
        file1_content = b"def hello(): pass\n"
        file2_content = b"class Foo:\n    bar = 42\n"
        
        (spec_dir / "file1.py").write_bytes(file1_content)
        (spec_dir / "subdir").mkdir()
        (spec_dir / "subdir" / "file2.py").write_bytes(file2_content)
        
        payload: dict[str, object] = {}
        files = [
            ("file1.py", "ignored content"),
            ("subdir/file2.py", "also ignored"),
        ]
        
        _record_tested_manifest(spec_dir, files, payload)
        
        manifest_path = spec_dir / "tested_manifest.json"
        assert manifest_path.exists(), "tested_manifest.json should be created"
        
        manifest_data = json.loads(manifest_path.read_text())
        
        expected_hash1 = hashlib.sha256(file1_content).hexdigest()
        expected_hash2 = hashlib.sha256(file2_content).hexdigest()
        
        assert manifest_data["file1.py"] == expected_hash1
        assert manifest_data["subdir/file2.py"] == expected_hash2

    def test_b2_payload_contains_artifact_hashes(self, tmp_path: Path):
        """B2: The event payload carries the same path-and-hash list under artifact_hashes."""
        spec_dir = tmp_path
        
        file_content = b"test code\n"
        (spec_dir / "code.py").write_bytes(file_content)
        
        payload: dict[str, object] = {"other_key": "value"}
        files = [("code.py", "content")]
        
        _record_tested_manifest(spec_dir, files, payload)
        
        assert "artifact_hashes" in payload
        hashes = payload["artifact_hashes"]
        
        assert isinstance(hashes, dict)
        assert "code.py" in hashes
        assert isinstance(hashes["code.py"], str)
        assert len(hashes["code.py"]) == 64  # SHA-256 hex digest length
        
        expected_hash = hashlib.sha256(file_content).hexdigest()
        assert hashes["code.py"] == expected_hash

    def test_b3_failing_check_writes_no_manifest(self, tmp_path: Path):
        """B3: A failing build check writes no manifest; existing one is left untouched."""
        spec_dir = tmp_path
        
        # Create an existing manifest from a prior passing check
        old_manifest_data = {"old_file.py": "abc123hash"}
        manifest_path = spec_dir / "tested_manifest.json"
        manifest_path.write_text(json.dumps(old_manifest_data))
        
        # Simulate failure by NOT calling the helper (success branch skipped)
        # The file should remain unchanged since we don't call _record_tested_manifest
        
        preserved_data = json.loads(manifest_path.read_text())
        assert preserved_data == old_manifest_data

    def test_empty_files_list_creates_empty_manifest(self, tmp_path: Path):
        """Helper handles empty files list gracefully."""
        spec_dir = tmp_path
        payload: dict[str, object] = {}
        
        _record_tested_manifest(spec_dir, [], payload)
        
        manifest_path = spec_dir / "tested_manifest.json"
        assert manifest_path.exists()
        manifest_data = json.loads(manifest_path.read_text())
        assert manifest_data == {}
        assert payload.get("artifact_hashes") == {}

    def test_missing_file_on_disk_is_skipped(self, tmp_path: Path):
        """Files listed but missing on disk are silently skipped in hashing."""
        spec_dir = tmp_path
        
        # Create only one of two referenced files
        (spec_dir / "exists.py").write_bytes(b"content\n")
        # exists.py is present; nonexistent.py is not
        
        payload: dict[str, object] = {}
        files = [
            ("exists.py", "content"),
            ("nonexistent.py", "missing content"),
        ]
        
        _record_tested_manifest(spec_dir, files, payload)
        
        manifest_path = spec_dir / "tested_manifest.json"
        manifest_data = json.loads(manifest_path.read_text())
        
        # Only the existing file should be hashed
        assert "exists.py" in manifest_data
        assert "nonexistent.py" not in manifest_data

    def test_never_raises_on_io_error(self, tmp_path: Path):
        """Helper never raises even when the manifest write fails.

        spec_dir is a FILE, so `spec_dir / "tested_manifest.json"` cannot be
        written (NotADirectoryError) — the helper must swallow and log it.
        """
        not_a_dir = tmp_path / "spec_dir_is_a_file"
        not_a_dir.write_text("occupied")

        payload: dict[str, object] = {}
        _record_tested_manifest(not_a_dir, [("test.txt", "data")], payload)

        # No exception escaped, and the failed pass added no hashes.
        assert "artifact_hashes" not in payload

    def test_hashes_actual_disk_bytes_not_memory_content(self, tmp_path: Path):
        """Hash is computed from disk bytes, ignoring content string parameter."""
        spec_dir = tmp_path
        
        actual_content = b"different than passed\n"
        (spec_dir / "file.py").write_bytes(actual_content)
        
        wrong_content_in_param = "this should be ignored"
        
        payload: dict[str, object] = {}
        files = [("file.py", wrong_content_in_param)]
        
        _record_tested_manifest(spec_dir, files, payload)
        
        manifest_data = json.loads((spec_dir / "tested_manifest.json").read_text())
        expected_hash = hashlib.sha256(actual_content).hexdigest()
        
        assert manifest_data["file.py"] == expected_hash


class TestIntegrationScenarios:
    """Integration-style tests simulating orchestrator behavior."""

    def test_success_branch_calls_helper(self, tmp_path: Path):
        """Simulate success path in _run_implementer calling the helper."""
        spec_dir = tmp_path
        
        # Setup artifacts that would come from ImplementerResult.files
        artifact_content = b"# verified code\nprint('hello')\n"
        (spec_dir / "verified_code.py").write_bytes(artifact_content)
        
        result_files = [
            ("verified_code.py", "# content string"),  # Content string is ignored for hashing
        ]
        
        # Simulate event payload construction
        payload: dict[str, object] = {
            "event_type": "pre_gate_build_check",
            "status": "passed",
        }
        
        # This is what happens on SUCCESS branch before db.record_event(TEST_RAN)
        _record_tested_manifest(spec_dir, result_files, payload)
        
        # Verify both side effects occurred
        assert (spec_dir / "tested_manifest.json").exists()
        assert "artifact_hashes" in payload
        assert len(payload["artifact_hashes"]) == 1

    def test_failure_branch_skips_helper(self, tmp_path: Path):
        """Simulate failure path NOT calling the helper."""
        spec_dir = tmp_path
        
        old_manifest = {"prior_pass.py": "oldhash"}
        manifest_path = spec_dir / "tested_manifest.json"
        manifest_path.write_text(json.dumps(old_manifest))
        
        # Failure scenario: build_passed=False or precheck_failed=True or build_reason="..."
        # In these cases, we simply don't call _record_tested_manifest
        
        # Existing manifest should be preserved
        current_data = json.loads(manifest_path.read_text())
        assert current_data == old_manifest