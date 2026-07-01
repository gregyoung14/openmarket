#!/usr/bin/env python3
"""Upload an OpenMarket dataset folder to Hugging Face Hub."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_hub import HfApi


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default="gregyoung14/openmarket-btc-polymarket")
    parser.add_argument("--folder", default="data/hf_release/sample_parquet")
    parser.add_argument("--path-in-repo", default="sample")
    parser.add_argument("--card", default="datasets/hf/README.md")
    parser.add_argument(
        "--manifest-json",
        default="data/hf_release/metadata/snapshot_manifest.json",
    )
    parser.add_argument(
        "--manifest-tsv",
        default="data/hf_release/metadata/snapshot_manifest.tsv",
    )
    parser.add_argument("--commit-message", default="Add OpenMarket sample dataset")
    parser.add_argument("--revision", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    folder = Path(args.folder)
    if not folder.exists():
        raise SystemExit(f"folder does not exist: {folder}")

    api = HfApi(token=os.environ.get("HF_TOKEN"))

    card = Path(args.card)
    if card.exists():
        api.upload_file(
            path_or_fileobj=str(card),
            path_in_repo="README.md",
            repo_id=args.repo_id,
            repo_type="dataset",
            commit_message="Add dataset card",
            revision=args.revision,
        )

    for local_path, remote_path in [
        (Path(args.manifest_json), "metadata/snapshot_manifest.json"),
        (Path(args.manifest_tsv), "metadata/snapshot_manifest.tsv"),
    ]:
        if local_path.exists():
            api.upload_file(
                path_or_fileobj=str(local_path),
                path_in_repo=remote_path,
                repo_id=args.repo_id,
                repo_type="dataset",
                commit_message=f"Add {remote_path}",
                revision=args.revision,
            )

    api.upload_folder(
        folder_path=str(folder),
        repo_id=args.repo_id,
        repo_type="dataset",
        path_in_repo=args.path_in_repo,
        commit_message=args.commit_message,
        revision=args.revision,
    )
    print(f"uploaded {folder} to {args.repo_id}/{args.path_in_repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
