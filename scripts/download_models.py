#!/usr/bin/env python3
"""Download new model GGUFs from HuggingFace using huggingface_hub.

Requires the `scripts` extra (`pip install -e ".[scripts]"`) — huggingface-hub
is not a runtime dependency of the server.

Supports resume on interrupt — safe to re-run if a download is interrupted.
Progress bars shown per-file via huggingface_hub's built-in tqdm integration.

Usage:
    python download_models.py           # Download all models
    python download_models.py 1 3       # Download only models 1 and 3
    python download_models.py --list    # Show models without downloading
"""
import os
import sys
from huggingface_hub import hf_hub_download, snapshot_download

MODELS_ROOT = os.path.expanduser("~/.lmstudio/models")
BASE_DIR = os.path.join(MODELS_ROOT, "unsloth")

MODELS = [
    {
        "id": 1,
        "name": "Qwen3.5-35B-A3B Q4_K_M",
        "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
        "files": ["Qwen3.5-35B-A3B-Q4_K_M.gguf"],
        "local_dir": os.path.join(BASE_DIR, "Qwen3.5-35B-A3B-GGUF"),
        "size": "~22 GB",
    },
    {
        "id": 2,
        "name": "Qwen3.5-122B-A10B Q4_K_M (3 shards)",
        "repo": "unsloth/Qwen3.5-122B-A10B-GGUF",
        "pattern": "Q4_K_M/*",
        "local_dir": os.path.join(BASE_DIR, "Qwen3.5-122B-A10B-GGUF"),
        "size": "~76.5 GB",
    },
    {
        "id": 3,
        "name": "Qwen3.5-397B-A17B IQ1_M (4 shards)",
        "repo": "unsloth/Qwen3.5-397B-A17B-GGUF",
        "pattern": "UD-IQ1_M/*",
        "local_dir": os.path.join(BASE_DIR, "Qwen3.5-397B-A17B-GGUF"),
        "size": "~100 GB",
    },
    {
        "id": 4,
        "name": "Nemotron-3-Nano-30B-A3B Q4_K_M",
        "repo": "unsloth/Nemotron-3-Nano-30B-A3B-GGUF",
        "files": ["Nemotron-3-Nano-30B-A3B-Q4_K_M.gguf"],
        "local_dir": os.path.join(BASE_DIR, "Nemotron-3-Nano-30B-A3B-GGUF"),
        "size": "~24.6 GB",
    },
    {
        "id": 5,
        "name": "GLM-4.7-Flash Q4_K_M",
        "repo": "unsloth/GLM-4.7-Flash-GGUF",
        "files": ["GLM-4.7-Flash-Q4_K_M.gguf"],
        "local_dir": os.path.join(BASE_DIR, "GLM-4.7-Flash-GGUF"),
        "size": "~18.3 GB",
    },
    {
        "id": 6,
        "name": "Qwen3.6-35B-A3B UD-Q4_K_M",
        "repo": "unsloth/Qwen3.6-35B-A3B-GGUF",
        "files": ["Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"],
        "local_dir": os.path.join(BASE_DIR, "Qwen3.6-35B-A3B-GGUF"),
        "size": "~22.1 GB",
    },
    {
        "id": 7,
        "name": "Qwen3.6-27B Q4_K_M (dense)",
        "repo": "unsloth/Qwen3.6-27B-GGUF",
        "files": ["Qwen3.6-27B-Q4_K_M.gguf"],
        "local_dir": os.path.join(BASE_DIR, "Qwen3.6-27B-GGUF"),
        "size": "~16.8 GB",
    },
    # Official GGUF from the lab, not an Unsloth requant — hence its own org dir.
    # arch is qwen35moe, which build 5343f45 already speaks (same as the 122B).
    # No longer wired to an agent: the `ornith` agent was retired 2026-07-14 after
    # losing the DEV-90 eval to `implementer`. Kept fetchable so DEV-98 (a Claude-
    # judged re-eval) can restore the config and re-run without re-hunting the repo.
    {
        "id": 8,
        "name": "Ornith-1.0-35B Q4_K_M (3B/35B MoE, MIT) — not in roster; see DEV-90",
        "repo": "deepreinforce-ai/Ornith-1.0-35B-GGUF",
        "files": ["ornith-1.0-35b-Q4_K_M.gguf"],
        "local_dir": os.path.join(MODELS_ROOT, "deepreinforce-ai", "Ornith-1.0-35B-GGUF"),
        "size": "~21.2 GB",
    },
]


def download_model(model):
    """Download a single model. Supports both single-file and snapshot (multi-shard) downloads."""
    print(f"\n{'='*60}")
    print(f"Downloading: {model['name']} ({model['size']})")
    print(f"Repo: {model['repo']}")
    print(f"Destination: {model['local_dir']}")
    print(f"{'='*60}")

    os.makedirs(model["local_dir"], exist_ok=True)

    if "files" in model:
        # Single-file download(s)
        for filename in model["files"]:
            print(f"\n  Downloading {filename}...")
            hf_hub_download(
                repo_id=model["repo"],
                filename=filename,
                local_dir=model["local_dir"],
                local_dir_use_symlinks=False,
            )
            print(f"  Done: {os.path.join(model['local_dir'], filename)}")
    elif "pattern" in model:
        # Multi-shard snapshot download
        print(f"\n  Downloading pattern: {model['pattern']}...")
        snapshot_download(
            repo_id=model["repo"],
            allow_patterns=model["pattern"],
            local_dir=model["local_dir"],
            local_dir_use_symlinks=False,
        )
        print(f"  Done: {model['local_dir']}")

    print(f"\nCompleted: {model['name']}")


def main():
    if "--list" in sys.argv:
        print("Available models:")
        for m in MODELS:
            print(f"  {m['id']}. {m['name']:45s} {m['size']:>10s}  ({m['repo']})")
        total = sum(float(m["size"].strip("~").split()[0]) for m in MODELS)
        print(f"\n  Total: ~{total:.0f} GB")
        return

    # Parse which models to download (default: all)
    selected_ids = set()
    for arg in sys.argv[1:]:
        try:
            selected_ids.add(int(arg))
        except ValueError:
            pass

    to_download = [m for m in MODELS if m["id"] in selected_ids] if selected_ids else MODELS

    print(f"Will download {len(to_download)} model(s):")
    for m in to_download:
        print(f"  {m['id']}. {m['name']} ({m['size']})")

    total_size = sum(float(m["size"].strip("~").split()[0]) for m in to_download)
    print(f"\nTotal: ~{total_size:.0f} GB")
    print("Downloads are resumable — safe to interrupt and re-run.\n")

    for m in to_download:
        try:
            download_model(m)
        except KeyboardInterrupt:
            print(f"\n\nInterrupted during {m['name']}. Re-run to resume.")
            sys.exit(1)
        except Exception as e:
            print(f"\nError downloading {m['name']}: {e}")
            print("Continuing with next model...\n")

    print(f"\n{'='*60}")
    print("All downloads complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
