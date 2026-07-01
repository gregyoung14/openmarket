#!/usr/bin/env python3
"""Merge and dedupe overlapping OpenMarket Parquet exports into a unified split.

Reads every table partition under `<input>_parquet/` (default: `full_parquet`),
deduplicates rows per table using the keys from the release investigation doc,
and writes a single timeline to `<output>_parquet/` (default: `unified_parquet`).

The merge runs per Hive date partition (`date=YYYY-MM-DD/`) so memory stays
bounded even for hundred-million-row tables.

Usage:
    .venv/bin/python scripts/datasets/merge_partitions.py
    .venv/bin/python scripts/datasets/merge_partitions.py --input-split full --tables binance_trades
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq


DEFAULT_ROOT = Path("data/hf_release")
DEFAULT_INPUT_SPLIT = "full"
DEFAULT_OUTPUT_SPLIT = "unified"

TABLE_CONFIG: dict[str, dict[str, Any]] = {
    "binance_trades": {
        "ts_col": "trade_time",
        "dedupe_cols": ["trade_id"],
        "order_cols": ["received_at", "trade_time"],
    },
    "binance_ticks_ms": {
        "ts_col": "source_ts_ms",
        "dedupe_cols": ["source_ts_ms", "trade_time_ms", "price", "volume"],
        "order_cols": ["ingest_ts_ms", "id"],
    },
    "polymarket_ticks_ms": {
        "ts_col": "source_ts_ms",
        "dedupe_cols": [
            "source_ts_ms",
            "market_slug",
            "asset_id",
            "side_label",
            "event_type",
            "price",
            "best_bid",
            "best_ask",
            "size",
        ],
        "order_cols": ["ingest_ts_ms", "id"],
    },
    "lag_pairs_ms": {
        "ts_col": "paired_at_ms",
        "dedupe_cols": [
            "paired_at_ms",
            "market_slug",
            "side_label",
            "binance_source_ts_ms",
            "polymarket_source_ts_ms",
            "polymarket_bid",
        ],
        "order_cols": ["id"],
    },
    "binance_candles_1s": {
        "ts_col": "candle_start",
        "dedupe_cols": ["candle_start"],
        "order_cols": ["created_at"],
    },
    "binance_candles_5s": {
        "ts_col": "candle_start",
        "dedupe_cols": ["candle_start"],
        "order_cols": ["created_at"],
    },
    "binance_candles_1m": {
        "ts_col": "candle_start",
        "dedupe_cols": ["candle_start"],
        "order_cols": ["created_at"],
    },
    "binance_candles_5m": {
        "ts_col": "candle_start",
        "dedupe_cols": ["candle_start"],
        "order_cols": ["created_at"],
    },
    "binance_candles_15m": {
        "ts_col": "candle_start",
        "dedupe_cols": ["candle_start"],
        "order_cols": ["created_at"],
    },
    "binance_candles_1h": {
        "ts_col": "candle_start",
        "dedupe_cols": ["candle_start"],
        "order_cols": ["created_at"],
    },
    "market_meta": {
        "ts_col": None,
        "dedupe_cols": ["market_slug"],
        "order_cols": ["last_seen_ms", "first_seen_ms"],
        "unpartitioned": True,
    },
    "crossover_alerts": {
        "ts_col": None,
        "dedupe_cols": ["id"],
        "order_cols": ["id"],
        "unpartitioned": True,
    },
}

DEFAULT_TABLES = list(TABLE_CONFIG)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=DEFAULT_ROOT, type=Path)
    parser.add_argument("--input-split", default=DEFAULT_INPUT_SPLIT)
    parser.add_argument("--output-split", default=DEFAULT_OUTPUT_SPLIT)
    parser.add_argument("--tables", nargs="+", default=DEFAULT_TABLES)
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--row-group-size", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--force", action="store_true",
                        help="rewrite output partitions even if they already exist")
    return parser.parse_args()


def quote_ident(name: str) -> str:
    return f'"{name}"'


def partition_dirs(table_dir: Path, unpartitioned: bool) -> list[Path]:
    if not table_dir.exists():
        return []
    if unpartitioned:
        unpart = table_dir / "unpartitioned"
        return [unpart if unpart.exists() else table_dir]
    return sorted(p for p in table_dir.iterdir() if p.is_dir() and p.name.startswith("date="))


def valid_parquet_files(part_dir: Path) -> list[Path]:
    """Return readable parquet files, skipping schema-only or corrupt shards."""
    valid: list[Path] = []
    for path in sorted(part_dir.glob("*.parquet")):
        try:
            meta = pq.read_metadata(str(path))
            if meta.num_columns > 0:
                valid.append(path)
        except Exception:
            print(f"    WARN: skipping unreadable parquet {path.name}", flush=True)
    return valid


def parquet_source(files: list[Path]) -> str:
    quoted = ", ".join(f"'{path}'" for path in files)
    return f"read_parquet([{quoted}], union_by_name=true)"


def dedupe_sql(source: str, dedupe_cols: list[str], order_cols: list[str]) -> str:
    partition_expr = ", ".join(quote_ident(c) for c in dedupe_cols)
    order_expr = ", ".join(f"{quote_ident(c)} DESC NULLS LAST" for c in order_cols)
    return f"""
        SELECT * EXCLUDE (rn)
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY {partition_expr}
                       ORDER BY {order_expr}
                   ) AS rn
            FROM {source}
        )
        WHERE rn = 1
    """


def merge_partition(
    con: duckdb.DuckDBPyConnection,
    input_files: list[Path],
    output_path: Path,
    dedupe_cols: list[str],
    order_cols: list[str],
    compression: str,
    row_group_size: int,
) -> tuple[int, int]:
    if not input_files:
        return 0, 0

    source = parquet_source(input_files)
    input_count = con.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0]
    if input_count == 0:
        return 0, 0

    deduped = dedupe_sql(source, dedupe_cols, order_cols)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({deduped}) TO '{output_path}' "
        f"(FORMAT PARQUET, COMPRESSION '{compression}', ROW_GROUP_SIZE {row_group_size})"
    )
    output_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{output_path}')"
    ).fetchone()[0]
    return input_count, output_count


def merge_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    input_root: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    cfg = TABLE_CONFIG[table]
    table_input = input_root / table
    table_output = output_root / table
    unpartitioned = bool(cfg.get("unpartitioned"))
    parts = partition_dirs(table_input, unpartitioned)

    report: dict[str, Any] = {
        "table": table,
        "input_partitions": len(parts),
        "input_rows": 0,
        "output_rows": 0,
        "duplicates_removed": 0,
        "output_parts": 0,
        "status": "ok",
    }

    if not parts:
        report["status"] = "missing"
        return report

    for part_dir in parts:
        input_files = valid_parquet_files(part_dir)
        if not input_files:
            continue

        if unpartitioned:
            out_dir = table_output / "unpartitioned"
            out_name = "part-000001.parquet"
        else:
            out_dir = table_output / part_dir.name
            out_name = "part-000001.parquet"

        out_path = out_dir / out_name
        if out_path.exists() and not args.force:
            source = parquet_source(input_files)
            input_count = con.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0]
            output_count = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{out_path}')"
            ).fetchone()[0]
            print(f"  skip {part_dir.name}: {out_path} exists ({output_count:,} rows)", flush=True)
        else:
            input_count, output_count = merge_partition(
                con,
                input_files,
                out_path,
                cfg["dedupe_cols"],
                cfg["order_cols"],
                args.compression,
                args.row_group_size,
            )
            removed = input_count - output_count
            print(
                f"  {part_dir.name}: {input_count:,} -> {output_count:,} "
                f"({removed:,} dupes) -> {out_path.name}",
                flush=True,
            )

        report["input_rows"] += input_count
        report["output_rows"] += output_count
        if output_count > 0:
            report["output_parts"] += 1

    report["duplicates_removed"] = report["input_rows"] - report["output_rows"]
    if report["input_rows"] and report["output_rows"] == 0:
        report["status"] = "empty"
    return report


def main() -> int:
    args = parse_args()
    input_root = args.root / f"{args.input_split}_parquet"
    output_root = args.root / f"{args.output_split}_parquet"

    if not input_root.exists():
        print(f"ERROR: {input_root} does not exist", file=sys.stderr)
        return 1

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "metadata").mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(":memory:")
    if args.threads > 0:
        con.execute(f"SET threads = {args.threads}")

    table_reports = []
    overall_t0 = time.perf_counter()
    for ti, table in enumerate(args.tables, 1):
        if table not in TABLE_CONFIG:
            print(f"WARN: unknown table {table}, skipping", file=sys.stderr)
            continue
        print(f"\n[{ti}/{len(args.tables)}] {table}", flush=True)
        t0 = time.perf_counter()
        report = merge_table(con, table, input_root, output_root, args)
        report["elapsed_seconds"] = round(time.perf_counter() - t0, 2)
        table_reports.append(report)

    con.close()
    elapsed = round(time.perf_counter() - overall_t0, 2)

    total_input = sum(r["input_rows"] for r in table_reports)
    total_output = sum(r["output_rows"] for r in table_reports)
    quality = {
        "split": args.output_split,
        "source_split": args.input_split,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "tables": len(table_reports),
        "input_rows": total_input,
        "output_rows": total_output,
        "duplicates_removed": total_input - total_output,
        "duplicate_rate": round((total_input - total_output) / total_input, 6) if total_input else 0.0,
        "per_table": table_reports,
        "dedupe_keys": {t: TABLE_CONFIG[t]["dedupe_cols"] for t in args.tables if t in TABLE_CONFIG},
    }

    quality_path = output_root / "metadata" / "merge_quality_report.json"
    quality_path.write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")

    # Link source export reports for provenance.
    src_meta = input_root / "metadata"
    dst_meta = output_root / "metadata"
    if src_meta.exists():
        for report in sorted(src_meta.glob("*.export_report.json")):
            link = dst_meta / report.name
            if not link.exists():
                link.write_bytes(report.read_bytes())

    print(f"\nwrote {quality_path}")
    print(json.dumps({
        "input_rows": total_input,
        "output_rows": total_output,
        "duplicates_removed": total_input - total_output,
        "duplicate_rate": quality["duplicate_rate"],
        "elapsed_seconds": elapsed,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())