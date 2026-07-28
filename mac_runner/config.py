"""Runtime configuration for mac_runner.

Env vars are loaded from ~/.config/coding-model-runner/.env (override via
CODING_MODEL_RUNNER_ENV_FILE). The registered-repo map lives at
~/.config/coding-model-runner/repos.yml.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path.home() / ".config" / "coding-model-runner"
ENV_FILE = Path(os.environ.get("CODING_MODEL_RUNNER_ENV_FILE", CONFIG_DIR / ".env"))
REPOS_FILE = Path(os.environ.get("CODING_MODEL_RUNNER_REPOS_FILE", CONFIG_DIR / "repos.yml"))


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overwriting existing keys."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(ENV_FILE)


class Config:
    HOST = os.getenv("CODING_MODEL_RUNNER_HOST", "127.0.0.1")
    PORT = int(os.getenv("CODING_MODEL_RUNNER_PORT", "5050"))
    API_KEY = os.getenv("CODING_MODEL_RUNNER_API_KEY", "")
    ALLOW_UNAUTH = os.getenv("CODING_MODEL_RUNNER_ALLOW_UNAUTH", "").lower() in ("1", "true", "yes")
    WORKTREE_ROOT = Path(os.getenv(
        "CODING_MODEL_RUNNER_WORKTREE_ROOT",
        str(Path.home() / "Library" / "Caches" / "coding-model-runner" / "worktrees"),
    ))
    DERIVED_DATA = Path(os.getenv(
        "CODING_MODEL_RUNNER_DERIVED_DATA",
        str(Path.home() / "Library" / "Caches" / "coding-model-runner" / "DerivedData"),
    ))
    REPOS_FILE = REPOS_FILE
    # Wrap swift/xcodebuild in sandbox-exec so LLM-authored build/test code
    # can't read credential material or write outside its build dirs
    # (DEV-126). On by default; set CODING_MODEL_RUNNER_SANDBOX=0 to disable
    # (e.g. while debugging a build the profile breaks).
    SANDBOX = os.getenv("CODING_MODEL_RUNNER_SANDBOX", "1").lower() not in (
        "0", "false", "no")
    SANDBOX_PROFILE = Path(os.getenv(
        "CODING_MODEL_RUNNER_SANDBOX_PROFILE",
        str(Path(__file__).parent / "sandbox.sb"),
    ))

    # Keychain the sandbox may read so codesign can reach the signing
    # identity's private key (DEV-398). Unset means no keychain is readable and
    # signed builds cannot work inside the sandbox. Point this at a DEDICATED
    # keychain containing only the signing certificate — the login keychain
    # holds every saved password on the machine and would be exposed wholesale
    # to LLM-authored test code. Create one with:
    #   security create-keychain -p '' build.keychain
    #   security import <cert>.p12 -k build.keychain -T /usr/bin/codesign
    #   security unlock-keychain -p '' build.keychain
    #   security list-keychains -d user -s build.keychain login.keychain-db
    SIGNING_KEYCHAIN = os.getenv("CODING_MODEL_RUNNER_SIGNING_KEYCHAIN", "")

    @classmethod
    def repos(cls) -> dict[str, Path]:
        """Load symbolic-name → absolute-path map from repos.yml."""
        if not cls.REPOS_FILE.is_file():
            return {}
        data = yaml.safe_load(cls.REPOS_FILE.read_text()) or {}
        out: dict[str, Path] = {}
        for name, entry in (data.get("repos") or {}).items():
            if not isinstance(entry, dict) or "path" not in entry:
                continue
            out[name] = Path(entry["path"]).expanduser().resolve()
        return out
