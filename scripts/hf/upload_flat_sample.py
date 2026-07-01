#!/usr/bin/env python3
"""Upload the OpenMarket sample split (flat layout) to the HF dataset repo.

Uploads:
  - every `*.parquet` under `data/hf_release/sample_flat/` to `<split>/`
  - the dataset card at `datasets/hf/README.md` to `README.md`
  - the redacted manifest under `data/hf_release/metadata_redacted/` to `metadata/`
  - the per-snapshot export reports under `data/hf_release/metadata/*.export_report.json`
    to `metadata/`

Idempotent: re-running on the same content uploads nothing (or only diffs).

Usage:
    .venv/bin/python scripts/hf/upload_flat_sample.py
    .venv/bin/python scripts/hf/upload_flat_sample.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi


DEFAULT_REPO = "gregyoung14/openmarket-btc-polymarket"
DEFAULT_SAMPLE_DIR = Path("data/hf_release/data_flat")
DEFAULT_MANIFEST_REDACTED = Path("data/hf_release/metadata_redacted")
DEFAULT_METADATA_DIR = Path("data/hf_release/metadata")
DEFAULT_CARD = Path("datasets/hf/README.md")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=DEFAULT_REPO)
    parser.add_argument("--sample-dir", default=DEFAULT_SAMPLE_DIR, type=Path)
    parser.add_argument("--manifest-redacted-dir", default=DEFAULT_MANIFEST_REDACTED, type=Path)
    parser.add_argument("--metadata-dir", default=DEFAULT_METADATA_DIR, type=Path)
    parser.add_argument("--card", default=DEFAULT_CARD, type=Path)
    parser.add_argument("--split-name", default="train",
                        help="HF split name in the dataset card YAML")
    parser.add_argument("--at-root", action="store_true",
                        help="Place parquet files at the repository root instead of under <split-name>/")
    parser.add_argument("--commit-message", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def stage_upload_dir(args: argparse.Namespace) -> tuple[Path, dict[str, int]]:
    """Assemble a single temp directory that mirrors the desired repo layout."""
    counts: dict[str, int] = {"parquet": 0, "metadata": 0, "sidecar": 0}
    staging = Path(tempfile.mkdtemp(prefix="openmarket_upload_"))
    parquet_root = staging if args.at_root else (staging / args.split_name)
    parquet_root.mkdir(parents=True, exist_ok=True)
    for p in sorted(args.sample_dir.glob("*.parquet")):
        shutil.copy2(p, parquet_root / p.name)
        counts["parquet"] += 1

    metadata_out = staging / "metadata"
    metadata_out.mkdir(parents=True, exist_ok=True)
    if args.manifest_redacted_dir.exists():
        for p in sorted(args.manifest_redacted_dir.glob("*")):
            shutil.copy2(p, metadata_out / p.name)
            counts["metadata"] += 1
    if args.metadata_dir.exists():
        for p in sorted(args.metadata_dir.glob("*.export_report.json")):
            shutil.copy2(p, metadata_out / p.name)
            counts["sidecar"] += 1

    return staging, counts


def main() -> int:
    args = parse_args()

    if not args.sample_dir.exists():
        print(f"ERROR: sample dir does not exist: {args.sample_dir}", flush=True)
        return 1
    if not args.card.exists():
        print(f"ERROR: dataset card does not exist: {args.card}", flush=True)
        return 1

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    if not api.token:
        print("WARNING: HF_TOKEN not set; falling back to cached auth", flush=True)

    staging, counts = stage_upload_dir(args)
    try:
        print(f"staged upload dir: {staging}")
        for kind, n in counts.items():
            print(f"  {kind}: {n}")
        print(f"repo: {args.repo_id}")
        print(f"card: {args.card}")

        commit = args.commit_message or (
            f"upload flat {args.split_name}/ split + redacted manifest "
            f"({counts['parquet']} parquet, {counts['metadata']} manifest files, "
            f"{counts['sidecar']} export reports)"
        )
        print(f"commit: {commit}")

        if args.dry_run:
            print("(dry-run: nothing uploaded)")
            for p in sorted(staging.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(staging)
                    print(f"  would upload: {rel}  ({p.stat().st_size:,} bytes)")
            return 0

        api.upload_folder(
            repo_id=args.repo_id,
            repo_type="dataset",
            folder_path=str(staging),
            commit_message=commit,
        )
        api.upload_file(
            path_or_fileobj=str(args.card),
            path_in_repo="README.md",
            repo_id=args.repo_id,
            repo_type="dataset",
            commit_description="update dataset card (flat sample layout + redacted manifest)",
        )

        summary = {
            "split": args.split_name,
            "repo_id": args.repo_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "parquet_files": counts["parquet"],
            "manifest_files": counts["metadata"],
            "sidecar_files": counts["sidecar"],
            "card": str(args.card),
        }
        log_path = args.sample_dir / "upload_flat_summary.json"
        log_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"summary -> {log_path}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())