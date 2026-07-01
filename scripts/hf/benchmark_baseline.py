#!/usr/bin/env python3
"""Record a benchmark baseline from the OpenMarket HF sample split.

Downloads `sample/` from `gregyoung14/openmarket-btc-polymarket`, measures
load time and per-table row counts, and writes a JSON + Markdown report.

Usage:
    .venv/bin/python scripts/hf/benchmark_baseline.py
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from huggingface_hub import snapshot_download
import pyarrow.parquet as pq


DEFAULT_REPO = "gregyoung14/openmarket-btc-polymarket"
DEFAULT_OUT_DIR = "benchmarks/baselines"
DEFAULT_LABEL = "v0.1-sample"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=DEFAULT_REPO)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    root = Path(snapshot_download(
        repo_id=args.repo_id,
        repo_type="dataset",
        allow_patterns=["sample/**", "metadata/**", "README.md"],
    ))
    download_seconds = time.perf_counter() - t0

    t1 = time.perf_counter()
    per_table: dict[str, dict[str, int]] = defaultdict(lambda: {"rows": 0, "parts": 0, "bytes": 0})
    total_rows = 0
    for pq_path in sorted(root.rglob("*.parquet")):
        rel = pq_path.relative_to(root)
        parts = rel.parts
        table = parts[1] if len(parts) > 2 and parts[0] == "sample" else parts[0]
        meta = pq.read_metadata(str(pq_path))
        per_table[table]["rows"] += meta.num_rows
        per_table[table]["parts"] += 1
        per_table[table]["bytes"] += pq_path.stat().st_size
        total_rows += meta.num_rows
    load_seconds = time.perf_counter() - t1

    summary = {
        "label": args.label,
        "repo_id": args.repo_id,
        "split": "sample",
        "snapshot": args.label,
        "download_seconds": round(download_seconds, 3),
        "load_seconds": round(load_seconds, 3),
        "total_rows": total_rows,
        "total_parquet_bytes": sum(v["bytes"] for v in per_table.values()),
        "tables": dict(sorted(per_table.items())),
    }

    json_path = out_dir / f"{args.label}.json"
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    md_path = out_dir / f"{args.label}.md"
    lines = [
        f"# Benchmark baseline — {args.label}",
        "",
        f"- repo: `{args.repo_id}`",
        f"- split: `sample`",
        f"- download_seconds: **{summary['download_seconds']}**",
        f"- load_seconds: **{summary['load_seconds']}**",
        f"- total_rows: **{summary['total_rows']:,}**",
        f"- total_parquet_bytes: **{summary['total_parquet_bytes']:,}**",
        "",
        "| table | rows | parts | bytes |",
        "|---|---:|---:|---:|",
    ]
    for name, info in sorted(per_table.items()):
        lines.append(f"| `{name}` | {info['rows']:,} | {info['parts']} | {info['bytes']:,} |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())