#!/usr/bin/env python3
"""End-to-end OpenMarket HF release: export -> validate -> upload -> bump.

Runs the multi-snapshot exporter, then validates the produced Parquet,
then uploads the new split to the dataset repo, then bumps the dataset
version. Idempotent: skips snapshots that already have an export report.

Usage:
    .venv/bin/python scripts/hf/release_split.py --split full \
        --max-snapshots 5 --new-version v0.2-full
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("sample", "full"), default="full")
    parser.add_argument("--repo-id", default="gregyoung14/openmarket-btc-polymarket")
    parser.add_argument("--max-snapshots", type=int, default=5)
    parser.add_argument("--min-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--chunk-rows", type=int, default=50_000)
    parser.add_argument("--new-version", required=True, help="new dataset version, e.g. v0.2-full")
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    return parser.parse_args()


def run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    args = parse_args()
    py = ".venv/bin/python"

    if not args.skip_export and args.split == "full":
        rc = run([
            py, "scripts/datasets/export_many_snapshots.py",
            "--max-snapshots", str(args.max_snapshots),
            "--min-bytes", str(args.min_bytes),
            "--chunk-rows", str(args.chunk_rows),
        ])
        if rc != 0:
            print("export failed", file=sys.stderr)
            return rc

    rc = run([
        py, "scripts/hf/validate_sample_split.py",
        "--sample-dir", args.split,
        "--repo-id", args.repo_id,
    ])
    if rc != 0:
        print(f"validate failed for {args.split}/", file=sys.stderr)
        return rc

    if not args.skip_upload:
        rc = run([
            py, "scripts/hf/upload_split.py",
            "--split", args.split,
            "--repo-id", args.repo_id,
            "--commit-message",
            f"upload {args.split}/ split (version {args.new_version})",
        ])
        if rc != 0:
            print("upload failed", file=sys.stderr)
            return rc

    rc = run([
        py, "scripts/hf/bump_dataset_version.py",
        "--set", args.new_version,
    ])
    if rc != 0:
        print("version bump failed", file=sys.stderr)
        return rc

    print(f"\nrelease complete: {args.split}/ -> {args.new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())