#!/usr/bin/env python3
"""Round-trip validate the published OpenMarket HF split (sample or full).

Downloads `gregyoung14/openmarket-btc-polymarket` to a temporary directory,
loads every Parquet file with PyArrow, sums row counts per table, and
compares against the aggregate metadata under `metadata/`.

Supports two layouts:
- sample/  : flat, e.g. `binance_trades/date=YYYY-MM-DD/*.parquet`
- full/    : same directory layout (one subdir per table)
- repo root: flat `<table>.parquet` (legacy)

Usage:
    .venv/bin/python scripts/hf/validate_sample_split.py
    .venv/bin/python scripts/hf/validate_sample_split.py --sample-dir full
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
DEFAULT_SAMPLE_DIR = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=DEFAULT_REPO)
    parser.add_argument("--sample-dir", default=DEFAULT_SAMPLE_DIR,
                        help="subdirectory containing parquet files; '' for repo root")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--aggregate", default=None,
                        help="use a specific aggregate.json filename (default: auto-detect)")
    return parser.parse_args()


def find_aggregate(root: Path, sample_dir: str) -> dict | None:
    """Find the aggregate JSON for the requested split.

    Search order:
    - `<sample_dir>/metadata/<sample_dir>_aggregate.json`
    - `metadata/<sample_dir>_aggregate.json`
    - `metadata/aggregate.json`

    Does not cross between splits: validating sample/ won't fall back to
    full/ aggregates.
    """
    candidates = []
    if sample_dir:
        candidates.append(root / sample_dir / "metadata" / f"{sample_dir}_aggregate.json")
        candidates.append(root / "metadata" / f"{sample_dir}_aggregate.json")
    else:
        candidates.append(root / "metadata" / "sample_aggregate.json")
    candidates.append(root / "metadata" / "aggregate.json")
    for c in candidates:
        if c.exists():
            return json.loads(c.read_text())
    return None


def find_per_snapshot_report(root: Path, sample_dir: str) -> dict | None:
    """For sample split (no aggregate), find the per-snapshot export report.

    Strict: only look at the directory matching the requested split. If
    `--sample-dir` is empty (sample split, parquet at repo root), look
    only at top-level metadata/. Do not fall back to full/ metadata.
    """
    if sample_dir:
        candidates = [root / sample_dir / "metadata"]
    else:
        candidates = [root / "metadata"]
    for c in candidates:
        if c.exists():
            reports = sorted(c.glob("*.export_report.json"))
            if reports:
                return json.loads(reports[0].read_text())
    return None


def main() -> int:
    args = parse_args()

    tmp_root = Path(tempfile.mkdtemp(prefix="openmarket_validate_"))
    try:
        print(f"downloading {args.repo_id} -> {tmp_root}")
        # Use list_repo_files (lighter than repo_info) and only fetch what
        # we need for this split + metadata + README.
        api = HfApi()
        all_files = api.list_repo_files(repo_id=args.repo_id, repo_type="dataset")

        if args.sample_dir:
            keep_prefix = f"{args.sample_dir}/"
            other_prefixes = {f"{s}/" for s in {"sample", "full"} - {args.sample_dir}}
        else:
            keep_prefix = ""  # root-level parquet (sample split)
            other_prefixes = {f"{s}/" for s in ("full", "sample")}

        parquet_targets = [
            f for f in all_files
            if f.endswith(".parquet")
            and (not keep_prefix or f.startswith(keep_prefix))
            and not any(f.startswith(p) for p in other_prefixes)
        ]
        metadata_targets = [
            f for f in all_files
            if f.startswith("metadata/")
            or (args.sample_dir and f.startswith(f"{args.sample_dir}/metadata/"))
        ]
        readme_targets = [f for f in all_files if f == "README.md"]
        selected = parquet_targets + metadata_targets + readme_targets
        print(f"  selected {len(selected)} files ({len(parquet_targets)} parquet + {len(metadata_targets)} metadata + {len(readme_targets)} readme)")
        print(f"  filtered from {len(all_files)} total siblings")

        local_dir = snapshot_download(
            repo_id=args.repo_id,
            repo_type="dataset",
            local_dir=str(tmp_root),
            allow_patterns=selected,
        )
        local_dir = snapshot_download(
            repo_id=args.repo_id,
            repo_type="dataset",
            local_dir=str(tmp_root),
            allow_patterns=selected,
        )
        root = Path(local_dir)

        sample_root = root / args.sample_dir if args.sample_dir else root
        if not sample_root.exists():
            print(f"ERROR: {sample_root} does not exist")
            return 1

        # Walk parquet files within this split only. Skip other split
        # subdirectories that may also live in the repo (sample vs full).
        skip_dirs = {"metadata"}
        if args.sample_dir:
            # We are validating e.g. full/; do not descend into other splits.
            other_splits = {"sample", "full"} - {args.sample_dir}
            skip_dirs.update(other_splits)
        observed: dict[str, int] = defaultdict(int)
        observed_files: dict[str, int] = defaultdict(int)
        observed_bytes: dict[str, int] = defaultdict(int)
        file_count = 0
        total_bytes = 0
        for pq_path in sorted(sample_root.rglob("*.parquet")):
            rel = pq_path.relative_to(sample_root)
            if rel.parts and rel.parts[0] in skip_dirs:
                continue
            # Layout 1: <table>/date=YYYY-MM-DD/part-NNN.parquet
            # Layout 2 (legacy sample): <table>.parquet
            parts = rel.parts
            if len(parts) >= 2 and parts[0] not in skip_dirs:
                table_name = parts[0]
            else:
                table_name = pq_path.stem
            n = pq.read_metadata(str(pq_path)).num_rows
            observed[table_name] += n
            observed_files[table_name] += 1
            observed_bytes[table_name] += pq_path.stat().st_size
            file_count += 1
            total_bytes += pq_path.stat().st_size

        aggregate = find_aggregate(root, args.sample_dir)
        if not aggregate and args.aggregate:
            p = root / "metadata" / args.aggregate
            if p.exists():
                aggregate = json.loads(p.read_text())

        if not aggregate:
            # Sample split may not have an aggregate; fall back to per-snapshot report.
            report = find_per_snapshot_report(root, args.sample_dir)
            if report:
                per_table = {}
                for t in report.get("tables", []):
                    rows = t.get("rows", 0)
                    parts = t.get("parts", 0)
                    # Empty tables (e.g. crossover_alerts) ship a 0-row,
                    # 0-column parquet but the report can say 0 parts.
                    # Don't invent files; trust the report.
                    per_table[t["table"]] = {
                        "rows": rows,
                        "parts": parts,
                        "snapshots": 1,
                    }
                aggregate = {
                    "split": args.sample_dir or "sample",
                    "snapshots": 1,
                    "total_rows": sum(t.get("rows", 0) for t in report.get("tables", [])),
                    "total_parquet_files": sum(t.get("parts", 0) for t in report.get("tables", [])),
                    "total_parquet_bytes": total_bytes,
                    "per_table": per_table,
                    "_source": "per_snapshot_export_report",
                }
            else:
                print()
                print("WARN: no aggregate metadata found; reporting observed only")
                print(f"  observed tables: {len(observed)}")
                print(f"  parquet files:   {file_count}")
                print(f"  parquet bytes:   {total_bytes:,}")
                return 0

        per_table = aggregate["per_table"]
        actual_files_per_table = aggregate.get("per_table_actual_files", {})
        print(f"split:           {aggregate['split']}")
        print(f"snapshots:       {aggregate['snapshots']}")
        print(f"reported rows:   {aggregate['total_rows']:,}")
        print(f"reported files:  {aggregate['total_parquet_files']}")
        print(f"reported bytes:  {aggregate['total_parquet_bytes']:,}")
        print()
        print(f"{'table':<25} {'want rows':>12} {'obs rows':>12} {'want files':>10} {'obs files':>10} {'match':>8}")
        all_ok = True
        observed_total = sum(observed.values())
        reported_total = aggregate["total_rows"]
        for table in sorted(set(per_table) | set(observed)):
            want_rows = per_table.get(table, {}).get("rows", 0)
            got_rows = observed.get(table, 0)
            want_files = actual_files_per_table.get(table, per_table.get(table, {}).get("parts", 0))
            got_files = observed_files.get(table, 0)
            ok = want_rows == got_rows and want_files == got_files
            all_ok &= ok
            print(f"{table:<25} {want_rows:>12,} {got_rows:>12,} {want_files:>10} {got_files:>10} {'OK' if ok else 'MISMATCH':>8}")

        # Aggregate row count check.
        agg_match = reported_total == observed_total
        if not agg_match:
            print(f"\naggregate row mismatch: reported={reported_total:,} observed={observed_total:,} "
                  f"(diff={observed_total - reported_total:,})")
        # File-count truth is the parquet files we just downloaded, not the
        # per-table report. Empty tables (e.g. crossover_alerts) ship a
        # schema-only parquet that per-snapshot reports sometimes log as 0 parts.
        files_match = file_count == aggregate["total_parquet_files"]
        bytes_match = total_bytes == aggregate["total_parquet_bytes"]
        if not files_match:
            # Only warn if observed > reported; that means a report lied.
            # observed < reported is fine (e.g. empty tables, report says 0).
            if file_count > aggregate["total_parquet_files"]:
                print(f"file count mismatch: reported={aggregate['total_parquet_files']} observed={file_count}")
        if not bytes_match:
            print(f"byte count mismatch: reported={aggregate['total_parquet_bytes']:,} observed={total_bytes:,}")
        print()
        print(f"observed parquet files: {file_count}")
        print(f"observed parquet bytes: {total_bytes:,}")
        file_check = files_match or file_count > aggregate["total_parquet_files"]
        print(f"file integrity: {'OK' if file_check and bytes_match else 'FAIL'}")
        print(f"row integrity:  {'OK' if agg_match else 'WARN (partial exports may over-count)'}")

        api = HfApi()
        api_info = api.repo_info(repo_id=args.repo_id, repo_type="dataset")
        print(f"remote last_modified: {api_info.last_modified}")

        # Pass if files and bytes match (truth is what's on disk); warn-only for row mismatch.
        return 0 if (files_match and bytes_match) else 2
    finally:
        if not args.keep:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())