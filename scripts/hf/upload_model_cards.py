#!/usr/bin/env python3
"""Upload OpenMarket model-card scaffolding to Hugging Face Hub."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_hub import HfApi


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default="gregyoung14/openmarket-models")
    parser.add_argument("--folder", default="models/hf")
    parser.add_argument("--commit-message", default="Add OpenMarket model card templates")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    folder = Path(args.folder)
    if not folder.exists():
        raise SystemExit(f"folder does not exist: {folder}")

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    api.upload_folder(
        folder_path=str(folder),
        repo_id=args.repo_id,
        repo_type="model",
        commit_message=args.commit_message,
    )
    print(f"uploaded {folder} to {args.repo_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
