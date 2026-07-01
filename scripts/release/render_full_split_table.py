#!/usr/bin/env python3
"""Render a per-snapshot row-count table from on-disk parquet truth.

Walks `data/hf_release/full_parquet/`, sums row counts per snapshot
(parsing the `<snapshot_id>-part-NNNNNN.parquet` filenames), and prints
a markdown table matching the layout in RELEASE-NOTES-v0.2.0.md.

Usage:
    .venv/bin/python scripts/release/render_full_split_table.py
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq


SNAPSHOT_RE = re.compile(r"^(polymarket_btc_data_\d{4}-\d{2}-\d{2}_\d{6})-")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/hf_release/full_parquet", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    per_snap: dict[str, dict[str, int]] = defaultdict(lambda: {"rows": 0, "parts": 0, "tables": set()})
    total_bytes = 0
    for pq_path in sorted(args.root.rglob("*.parquet")):
        rel = pq_path.relative_to(args.root)
        if rel.parts[0] == "metadata":
            continue
        table = rel.parts[0]
        m = SNAPSHOT_RE.match(pq_path.name)
        snap = m.group(1) if m else "unknown"
        meta = pq.read_metadata(str(pq_path))
        per_snap[snap]["rows"] += meta.num_rows
        per_snap[snap]["parts"] += 1
        per_snap[snap]["tables"].add(table)
        total_bytes += pq_path.stat().st_size

    print(f"\n# Full split — {len(per_snap)} snapshots, {sum(v['rows'] for v in per_snap.values()):,} rows, {total_bytes:,} bytes\n")
    print("| snapshot_id | rows | parts | tables |")
    print("|---|---:|---:|---:|")
    for snap in sorted(per_snap):
        info = per_snap[snap]
        print(f"| `{snap}` | {info['rows']:,} | {info['parts']} | {len(info['tables'])} |")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())