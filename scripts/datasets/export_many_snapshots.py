#!/usr/bin/env python3
"""Export multiple OpenMarket SQLite snapshots to a unified Parquet split.

Resolves snapshot filenames through the manifest, runs the per-snapshot
exporter for each, and aggregates per-snapshot reports into a single
multi-export report. Skips snapshots that already have an export report
unless --force is passed.

Usage:
    .venv/bin/python scripts/datasets/export_many_snapshots.py \
        --out-dir data/hf_release/full_parquet \
        --max-snapshots 5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_OUT_DIR = "data/hf_release/full_parquet"
DEFAULT_STAGING = "data/hf_release/staging"
EXPORTER = "scripts/datasets/export_snapshot_to_parquet.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--chunk-rows", type=int, default=50_000)
    parser.add_argument("--max-snapshots", type=int, default=10)
    parser.add_argument("--min-bytes", type=int, default=10 * 1024 * 1024,
                        help="Only export snapshots at least this large (skip tiny residue)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--python", default=".venv/bin/python")
    return parser.parse_args()


def existing_reports(out_dir: Path) -> set[str]:
    meta = out_dir / "metadata"
    if not meta.exists():
        return set()
    return {p.stem.replace(".export_report", "") for p in meta.glob("*.export_report.json")}


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    snapshots = manifest["snapshots"]

    candidates = sorted(
        (s for s in snapshots if s["compressed_bytes"] >= args.min_bytes),
        key=lambda s: s["compressed_bytes"],
        reverse=True,
    )
    candidates = candidates[: args.max_snapshots]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = existing_reports(out_dir) if not args.force else set()

    selected = [s for s in candidates if s["filename"].removesuffix(".db.gz") not in done]
    print(f"manifest snapshots: {len(snapshots)}")
    print(f"candidates (>= {args.min_bytes:,} bytes): {len(candidates)}")
    print(f"already exported (skipping): {len(done)}")
    print(f"will export: {len(selected)}")

    for snap in selected:
        print(f"\n=== {snap['filename']} ({snap['compressed_bytes']:,} bytes) ===")
        cmd = [
            args.python, EXPORTER, snap["filename"],
            "--manifest", args.manifest,
            "--out-dir", args.out_dir,
            "--staging-dir", args.staging_dir,
            "--chunk-rows", str(args.chunk_rows),
        ]
        if args.keep_db:
            cmd.append("--keep-db")
        result = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode != 0:
            print(f"FAILED: {snap['filename']} (rc={result.returncode})", file=sys.stderr)
            continue

    agg_path = out_dir / "metadata" / "full_export_summary.json"
    summary = {
        "manifest_snapshots": len(snapshots),
        "candidates": len(candidates),
        "exported_reports": sorted(existing_reports(out_dir)),
    }
    agg_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary -> {agg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())