#!/usr/bin/env python3
"""Create Hugging Face Hub repositories for OpenMarket.

Requires authentication via `HF_TOKEN` or `hf auth login`.
"""

from __future__ import annotations

import argparse
import os

from huggingface_hub import HfApi


DEFAULT_CODE_REPO = "gregyoung14/openmarket"
DEFAULT_DATASET_REPO = "gregyoung14/openmarket-btc-polymarket"
DEFAULT_MODEL_REPO = "gregyoung14/openmarket-models"
DEFAULT_SPACE_REPO = "gregyoung14/openmarket-demo"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-repo", default=DEFAULT_DATASET_REPO)
    parser.add_argument("--model-repo", default=DEFAULT_MODEL_REPO)
    parser.add_argument("--space-repo", default=DEFAULT_SPACE_REPO)
    parser.add_argument("--include-space", action="store_true")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--exist-ok", action="store_true", default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    repos = [
        (args.dataset_repo, "dataset"),
        (args.model_repo, "model"),
    ]
    if args.include_space:
        repos.append((args.space_repo, "space"))

    for repo_id, repo_type in repos:
        print(f"creating {repo_type} repo: {repo_id}")
        api.create_repo(
            repo_id=repo_id,
            repo_type=repo_type,
            private=args.private,
            exist_ok=args.exist_ok,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
