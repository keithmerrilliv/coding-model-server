"""Temporary file tracking and cleanup."""
import os
import time
import atexit

from coding_model_client.config import COLORS, print_colored

# Maps path -> creation_time
_temp_files = {}


def _cleanup_temp_files():
    """Remove all tracked temporary files on exit."""
    for path in list(_temp_files.keys()):
        try:
            if os.path.exists(path):
                os.remove(path)
        except PermissionError as e:
            print_colored(f"Permission denied removing temp file {path}: {e}", COLORS["WARNING"])
        except OSError as e:
            print_colored(f"OS error removing temp file {path}: {e}", COLORS["WARNING"])
        except Exception as e:
            print_colored(f"Unexpected error removing temp file {path}: {e}", COLORS["FAIL"])
    _temp_files.clear()


def _cleanup_old_temp_files(max_age_minutes=60):
    """Remove temporary files older than max_age_minutes."""
    current_time = time.time()
    expired_files = []
    for path, creation_time in _temp_files.items():
        if current_time - creation_time > max_age_minutes * 60:
            expired_files.append(path)
    for path in expired_files:
        try:
            if os.path.exists(path):
                os.remove(path)
                print_colored(f"Cleaned up old temp file: {path}", COLORS['WARNING'])
        except Exception as e:
            print_colored(f"Failed to clean up temp file {path}: {e}", COLORS['FAIL'])
        finally:
            _temp_files.pop(path, None)


def _add_temp_file(path):
    """Add a temporary file to tracking with timestamp."""
    _temp_files[path] = time.time()
    if len(_temp_files) % 10 == 0:
        _cleanup_old_temp_files()


def _remove_temp_file(path):
    """Remove a temporary file from tracking."""
    _temp_files.pop(path, None)


atexit.register(_cleanup_temp_files)
