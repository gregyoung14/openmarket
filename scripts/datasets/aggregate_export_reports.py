#!/usr/bin/env python3
"""Aggregate per-snapshot export reports into a single split summary.

Reads every `*.export_report.json` under `<split>_parquet/metadata/` for
snapshot-level metadata (engines, source files, statuses), then walks the
actual Parquet tree to compute ground-truth row counts, file counts, and
total bytes. The per-snapshot `export_report.json` row counts are kept
as `reported_rows` for reconciliation; the summary's `total_rows` and
`per_table.rows` reflect the real on-disk totals.

Usage:
    .venv/bin/python scripts/datasets/aggregate_export_reports.py --split full
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq


DEFAULT_ROOT = Path("data/hf_release")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("sample", "full", "unified"), default="full")
    parser.add_argument("--root", default=DEFAULT_ROOT, type=Path)
    parser.add_argument("--no-actuals", action="store_true",
                        help="skip parquet scan (faster but reported_rows only)")
    return parser.parse_args()


def scan_parquet(parquet_root: Path) -> tuple[dict[str, int], int]:
    """Walk the parquet tree and return (per_table_rows, total_bytes)."""
    per_table_rows: dict[str, int] = defaultdict(int)
    total_bytes = 0
    for pq_path in parquet_root.rglob("*.parquet"):
        rel = pq_path.relative_to(parquet_root)
        parts = rel.parts
        if len(parts) == 0 or parts[0] == "metadata":
            continue
        table = parts[0]
        try:
            meta = pq.read_metadata(str(pq_path))
            per_table_rows[table] += meta.num_rows
        except Exception:
            pass
        total_bytes += pq_path.stat().st_size
    return dict(per_table_rows), total_bytes


def main() -> int:
    args = parse_args()
    meta_dir = args.root / f"{args.split}_parquet" / "metadata"
    if not meta_dir.exists():
        print(f"ERROR: {meta_dir} does not exist")
        return 1

    quality_path = meta_dir / "merge_quality_report.json"
    reports = sorted(meta_dir.glob("*.export_report.json"))

    reported_per_table: dict[str, dict[str, int]] = defaultdict(
        lambda: {"rows": 0, "parts": 0, "snapshots": 0}
    )
    snapshot_summaries = []

    if args.split == "unified" and quality_path.exists():
        quality = json.loads(quality_path.read_text())
        for entry in quality.get("per_table", []):
            table = entry["table"]
            reported_per_table[table]["rows"] = entry.get("output_rows", 0)
            reported_per_table[table]["parts"] = entry.get("output_parts", 0)
            reported_per_table[table]["snapshots"] = 1
        snapshot_summaries.append({
            "snapshot_id": "unified-merge",
            "snapshot": None,
            "reported_rows": quality.get("output_rows", 0),
            "reported_parts": sum(e.get("output_parts", 0) for e in quality.get("per_table", [])),
            "engine": "merge_partitions",
            "integrity_status": "ok",
        })
    elif not reports:
        print(f"ERROR: no export reports in {meta_dir}")
        return 1
    else:
        pass

    for r in (reports if args.split != "unified" or not quality_path.exists() else []):
        data = json.loads(r.read_text())
        snapshot_id = data.get("snapshot_id", r.stem.replace(".export_report", ""))
        snap_rows = 0
        snap_parts = 0
        for t in data.get("tables", []):
            table = t["table"]
            reported_per_table[table]["rows"] += t.get("rows", 0)
            reported_per_table[table]["parts"] += t.get("parts", 0)
            reported_per_table[table]["snapshots"] += 1
            snap_rows += t.get("rows", 0)
            snap_parts += t.get("parts", 0)
        snapshot_summaries.append({
            "snapshot_id": snapshot_id,
            "snapshot": data.get("snapshot"),
            "reported_rows": snap_rows,
            "reported_parts": snap_parts,
            "engine": data.get("engine"),
            "integrity_status": data.get("integrity_status"),
        })

    parquet_root = args.root / f"{args.split}_parquet"
    actual_per_table_files: dict[str, int] = defaultdict(int)
    for pq_path in parquet_root.rglob("*.parquet"):
        rel = pq_path.relative_to(parquet_root)
        if len(rel.parts) and rel.parts[0] != "metadata":
            actual_per_table_files[rel.parts[0]] += 1

    if args.no_actuals:
        actual_per_table_rows = {}
        actual_total_bytes = 0
    else:
        actual_per_table_rows, actual_total_bytes = scan_parquet(parquet_root)

    actual_per_table: dict[str, dict[str, int]] = {}
    for table in sorted(set(reported_per_table) | set(actual_per_table_files) | set(actual_per_table_rows)):
        actual_per_table[table] = {
            "rows": actual_per_table_rows.get(table, 0),
            "parts": actual_per_table_files.get(table, 0),
            "snapshots": reported_per_table.get(table, {}).get("snapshots", 0),
            "reported_rows": reported_per_table.get(table, {}).get("rows", 0),
            "reported_parts": reported_per_table.get(table, {}).get("parts", 0),
        }

    summary = {
        "split": args.split,
        "snapshots": len(snapshot_summaries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_rows": sum(v["rows"] for v in actual_per_table.values()),
        "total_reported_rows": sum(v["reported_rows"] for v in actual_per_table.values()),
        "total_parquet_files": sum(actual_per_table_files.values()),
        "total_parquet_bytes": actual_total_bytes,
        "per_table": actual_per_table,
        "snapshots_detail": snapshot_summaries,
    }

    out_json = meta_dir / f"{args.split}_aggregate.json"
    out_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    out_md = meta_dir / f"{args.split}_aggregate.md"
    lines = [
        f"# {args.split}/ aggregate — {summary['snapshots']} snapshot(s)",
        "",
        f"- total_rows: **{summary['total_rows']:,}** (from parquet files)",
        f"- total_reported_rows: **{summary['total_reported_rows']:,}** (from per-snapshot reports)",
        f"- total_parquet_files: **{summary['total_parquet_files']}**",
        f"- total_parquet_bytes: **{summary['total_parquet_bytes']:,}**",
        "",
        "| table | rows | parts | snapshots | reported_rows |",
        "|---|---:|---:|---:|---:|",
    ]
    for t, info in sorted(actual_per_table.items()):
        lines.append(
            f"| `{t}` | {info['rows']:,} | {info['parts']} | {info['snapshots']} | {info['reported_rows']:,} |"
        )
    lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    print(json.dumps({k: summary[k] for k in ("total_rows", "total_reported_rows", "total_parquet_files", "total_parquet_bytes")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())