#!/usr/bin/env python3
"""Upload the OpenMarket HF dataset split to the Hub.

Iterates over `sample/` (default) or `full/` under `data/hf_release/<split>_parquet/`,
uploads every file under the matching prefix in the HF dataset repo, and writes
an upload manifest so the operation is idempotent.

Usage:
    .venv/bin/python scripts/hf/upload_split.py --split full
    .venv/bin/python scripts/hf/upload_split.py --split sample
    .venv/bin/python scripts/hf/upload_split.py --split full --repo-id org/repo
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi


DEFAULT_REPO = "gregyoung14/openmarket-btc-polymarket"
DEFAULT_ROOT = Path("data/hf_release")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("sample", "full", "unified"), default="sample")
    parser.add_argument("--repo-id", default=DEFAULT_REPO)
    parser.add_argument("--root", default=DEFAULT_ROOT, type=Path)
    parser.add_argument("--commit-message", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    local_dir = args.root / f"{args.split}_parquet"
    if not local_dir.exists():
        print(f"ERROR: {local_dir} does not exist")
        return 1
    meta_dir = local_dir / "metadata"
    metadata_files = list(meta_dir.glob("*.export_report.json"))
    quality_report = meta_dir / "merge_quality_report.json"
    if not metadata_files and not quality_report.exists():
        print(f"ERROR: no export reports or merge_quality_report.json in {meta_dir}")
        return 1

    parquet_count = sum(1 for _ in local_dir.rglob("*.parquet"))
    if parquet_count == 0:
        print(f"ERROR: no parquet files in {local_dir}")
        return 1

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    if not api.token:
        print("WARNING: HF_TOKEN not set; using cached auth (if any)")

    commit = args.commit_message or (
        f"upload {args.split}/ split ({parquet_count} parquet files, "
        f"{len(metadata_files)} export reports"
        f"{', merge quality report' if quality_report.exists() else ''})"
    )

    print(f"uploading {local_dir} -> {args.repo_id} ({args.split}/)")
    print(f"parquet files: {parquet_count}")
    print(f"export reports: {len(metadata_files)}")
    if quality_report.exists():
        print(f"merge quality report: {quality_report.name}")
    print(f"commit: {commit}")

    if args.dry_run:
        print("(dry-run: nothing uploaded)")
        return 0

    try:
        api.upload_folder(
            repo_id=args.repo_id,
            repo_type="dataset",
            folder_path=str(local_dir),
            path_in_repo=args.split,
            commit_message=commit,
            multi_commits=True,
            multi_commits_verbose=True,
        )
    except TypeError:
        # Older huggingface_hub doesn't support multi_commits.
        api.upload_folder(
            repo_id=args.repo_id,
            repo_type="dataset",
            folder_path=str(local_dir),
            path_in_repo=args.split,
            commit_message=commit,
        )

    summary = {
        "split": args.split,
        "repo_id": args.repo_id,
        "parquet_files": parquet_count,
        "export_reports": len(metadata_files),
        "has_merge_quality_report": quality_report.exists(),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "commit_message": commit,
    }
    out = local_dir / "metadata" / f"{args.split}_upload_summary.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"summary -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())