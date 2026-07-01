#!/usr/bin/env python3
"""Round-trip validate the published OpenMarket HF sample split.

Downloads `gregyoung14/openmarket-btc-polymarket` to a temporary directory,
loads every Parquet file with PyArrow, checks row counts against the export
report that ships in the dataset, and prints a summary.

Usage:
    .venv/bin/python scripts/hf/validate_sample_split.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download
import pyarrow.parquet as pq


DEFAULT_REPO = "gregyoung14/openmarket-btc-polymarket"
DEFAULT_SAMPLE_DIR = "sample"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=DEFAULT_REPO)
    parser.add_argument("--sample-dir", default=DEFAULT_SAMPLE_DIR)
    parser.add_argument("--keep", action="store_true", help="keep downloaded files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    tmp_root = Path(tempfile.mkdtemp(prefix="openmarket_validate_"))
    try:
        print(f"downloading {args.repo_id} (sample split) -> {tmp_root}")
        local_dir = snapshot_download(
            repo_id=args.repo_id,
            repo_type="dataset",
            local_dir=str(tmp_root),
            allow_patterns=[
                f"{args.sample_dir}/**",
                "metadata/**",
                "README.md",
            ],
        )
        root = Path(local_dir)

        report_files = list((root / args.sample_dir / "metadata").glob("*.export_report.json"))
        if not report_files:
            report_files = list((root / "metadata").glob("*.export_report.json"))
        if not report_files:
            print("ERROR: no export_report.json found in sample/metadata/ or top-level metadata/")
            return 1
        report = json.loads(report_files[0].read_text())
        snapshot = report["snapshot"]
        print(f"snapshot: {snapshot}")

        expected = {entry["table"]: entry["rows"] for entry in report["tables"] if entry.get("exists")}

        observed: dict[str, int] = defaultdict(int)
        file_count = 0
        total_bytes = 0
        for pq_path in sorted(root.rglob("*.parquet")):
            rel = pq_path.relative_to(root)
            if rel.parts[0] != args.sample_dir:
                continue
            table = pq.read_table(pq_path)
            table_name = rel.stem
            observed[table_name] += table.num_rows
            file_count += 1
            total_bytes += pq_path.stat().st_size

        print()
        print(f"{'table':<25} {'expected':>10} {'observed':>10} {'match':>8}")
        all_ok = True
        for table, want in expected.items():
            got = observed.get(table, 0)
            ok = want == got
            all_ok &= ok
            print(f"{table:<25} {want:>10} {got:>10} {'OK' if ok else 'MISMATCH':>8}")
        extra = set(observed) - set(expected)
        if extra:
            print(f"\nunexpected tables observed (not in report): {sorted(extra)}")
            all_ok = False
        print()
        print(f"parquet files: {file_count}")
        print(f"parquet bytes: {total_bytes}")
        print(f"status: {'PASS' if all_ok else 'FAIL'}")

        api = HfApi()
        api_info = api.repo_info(repo_id=args.repo_id, repo_type="dataset")
        print(f"remote last_modified: {api_info.last_modified}")

        return 0 if all_ok else 2
    finally:
        if not args.keep:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())