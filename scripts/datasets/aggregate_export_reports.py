#!/usr/bin/env python3
"""Aggregate per-snapshot export reports into a single split summary.

Reads every `*.export_report.json` under `<split>_parquet/metadata/`, sums
table row counts and parquet bytes, and writes an aggregated JSON + Markdown
report under `<split>_parquet/metadata/`.

Usage:
    .venv/bin/python scripts/datasets/aggregate_export_reports.py --split full
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path("data/hf_release")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("sample", "full"), default="full")
    parser.add_argument("--root", default=DEFAULT_ROOT, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    meta_dir = args.root / f"{args.split}_parquet" / "metadata"
    if not meta_dir.exists():
        print(f"ERROR: {meta_dir} does not exist")
        return 1

    reports = sorted(meta_dir.glob("*.export_report.json"))
    if not reports:
        print(f"ERROR: no export reports in {meta_dir}")
        return 1

    per_table: dict[str, dict[str, int]] = defaultdict(
        lambda: {"rows": 0, "parts": 0, "snapshots": 0}
    )
    snapshot_summaries = []
    total_parquet_bytes = 0

    for r in reports:
        data = json.loads(r.read_text())
        snapshot_id = data.get("snapshot_id", r.stem.replace(".export_report", ""))
        snap_rows = 0
        snap_parts = 0
        for t in data.get("tables", []):
            table = t["table"]
            per_table[table]["rows"] += t.get("rows", 0)
            per_table[table]["parts"] += t.get("parts", 0)
            per_table[table]["snapshots"] += 1
            snap_rows += t.get("rows", 0)
            snap_parts += t.get("parts", 0)
        snapshot_summaries.append({
            "snapshot_id": snapshot_id,
            "snapshot": data.get("snapshot"),
            "rows": snap_rows,
            "parts": snap_parts,
            "engine": data.get("engine"),
        })

    # Compute parquet bytes and file counts from actual files (truth source).
    parquet_root = args.root / f"{args.split}_parquet"
    actual_files_per_table: dict[str, int] = defaultdict(int)
    for pq in parquet_root.rglob("*.parquet"):
        total_parquet_bytes += pq.stat().st_size
        rel = pq.relative_to(parquet_root)
        parts = rel.parts
        if len(parts) >= 1 and parts[0] != "metadata":
            actual_files_per_table[parts[0]] += 1

    summary = {
        "split": args.split,
        "snapshots": len(snapshot_summaries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_rows": sum(v["rows"] for v in per_table.values()),
        "total_parquet_files": sum(actual_files_per_table.values()),
        "total_parquet_bytes": total_parquet_bytes,
        "per_table": {k: dict(v) for k, v in sorted(per_table.items())},
        "per_table_actual_files": dict(actual_files_per_table),
        "snapshots_detail": snapshot_summaries,
    }

    out_json = meta_dir / f"{args.split}_aggregate.json"
    out_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    out_md = meta_dir / f"{args.split}_aggregate.md"
    lines = [
        f"# {args.split}/ aggregate — {summary['snapshots']} snapshot(s)",
        "",
        f"- total_rows: **{summary['total_rows']:,}**",
        f"- total_parquet_files: **{summary['total_parquet_files']}**",
        f"- total_parquet_bytes: **{summary['total_parquet_bytes']:,}**",
        "",
        "| table | rows | parts | snapshots |",
        "|---|---:|---:|---:|",
    ]
    for t, info in sorted(per_table.items()):
        lines.append(f"| `{t}` | {info['rows']:,} | {info['parts']} | {info['snapshots']} |")
    lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    print(json.dumps({k: summary[k] for k in ("total_rows", "total_parquet_files", "total_parquet_bytes")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())