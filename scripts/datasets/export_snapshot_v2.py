#!/usr/bin/env python3
"""Fastest SQLite -> partitioned Parquet exporter using DuckDB native tables.

Strategy:
  1. ATTACH the SQLite and COPY each table into a native DuckDB in-memory table
     (one pass over the data; DuckDB stores it columnar and compressed).
  2. For each table, find distinct dates (fast against native DuckDB).
  3. Write each date partition as a separate Parquet file via COPY.
  4. If DuckDB can't attach the SQLite at all, fall back to per-table sqlite3 reads.

This is ~10x faster than iterating SQLite directly because partitioning,
filtering, and sorting happen on native columnar storage.

Usage:
    .venv/bin/python scripts/datasets/export_snapshot_v2.py \
        polymarket_btc_data_2026-03-14_193215.db.gz \
        --manifest data/hf_release/metadata/snapshot_manifest.json \
        --out-dir data/hf_release/full_parquet \
        --staging-dir data/hf_release/staging
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import duckdb


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_STAGING = "data/hf_release/staging"
DEFAULT_OUT = "data/hf_release/parquet"

TABLE_TIMESTAMP_COLUMNS = {
    "binance_trades": "trade_time",
    "binance_ticks_ms": "source_ts_ms",
    "polymarket_ticks_ms": "source_ts_ms",
    "lag_pairs_ms": "paired_at_ms",
    "binance_candles_1s": "candle_start",
    "binance_candles_5s": "candle_start",
    "binance_candles_1m": "candle_start",
    "binance_candles_5m": "candle_start",
    "binance_candles_15m": "candle_start",
    "binance_candles_1h": "candle_start",
}
DEFAULT_TABLES = list(TABLE_TIMESTAMP_COLUMNS) + ["market_meta", "crossover_alerts"]
RAW_JSON_COLUMN = "raw_json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--tables", nargs="+", default=DEFAULT_TABLES)
    parser.add_argument("--include-raw-json", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--row-group-size", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=0,
                        help="DuckDB threads (0 = use all cores)")
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"snapshots": []}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_snapshot(snapshot: str, manifest_path: Path) -> tuple[str, str]:
    if Path(snapshot).exists():
        return snapshot, Path(snapshot).name
    if snapshot.startswith(("http://", "https://")):
        return snapshot, Path(snapshot).name
    for item in load_manifest(manifest_path).get("snapshots", []):
        if item.get("filename") == snapshot:
            return item["public_url"], item["filename"]
    raise RuntimeError(f"could not resolve snapshot: {snapshot}")


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    print(f"  GET {url}", file=sys.stderr, flush=True)
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
        total = int(response.headers.get("Content-Length", "0") or 0)
        downloaded = 0
        chunk = 1024 * 1024
        last_pct = -1
        while True:
            buf = response.read(chunk)
            if not buf:
                break
            out.write(buf)
            downloaded += len(buf)
            if total:
                pct = downloaded * 100 // total
                if pct != last_pct and pct % 5 == 0:
                    print(
                        f"    {downloaded / 1024**2:,.1f} / {total / 1024**2:,.1f} MiB ({pct}%)",
                        file=sys.stderr, flush=True,
                    )
                    last_pct = pct
    print(f"  done -> {tmp} ({tmp.stat().st_size / 1024**2:,.1f} MiB)", file=sys.stderr, flush=True)
    tmp.replace(destination)


def materialize_db(source: str, filename: str, staging_dir: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(source)
    compressed = source_path if source_path.exists() else staging_dir / filename
    if not compressed.exists():
        print(f"downloading {source}", file=sys.stderr, flush=True)
        download(source, compressed)
    if compressed.suffix != ".gz":
        return compressed
    db_path = staging_dir / compressed.name.removesuffix(".gz")
    if db_path.exists():
        return db_path
    src_size = compressed.stat().st_size
    print(
        f"decompressing {compressed.name} ({src_size / 1024**2:,.1f} MiB compressed) -> {db_path.name}",
        file=sys.stderr, flush=True,
    )
    in_bytes = 0
    out_bytes = 0
    chunk = 1024 * 1024
    last_pct = -1
    with gzip.open(compressed, "rb") as src, db_path.open("wb") as dst:
        while True:
            buf = src.read(chunk)
            if not buf:
                break
            in_bytes += len(buf)
            dst.write(buf)
            out_bytes = dst.tell()
            pct = in_bytes * 100 // src_size if src_size else 0
            if pct != last_pct and pct % 20 == 0:
                print(
                    f"  input {pct}% ({in_bytes / 1024**2:,.1f} / {src_size / 1024**2:,.1f} MiB) "
                    f"-> {out_bytes / 1024**2:,.1f} MiB written",
                    file=sys.stderr, flush=True,
                )
                last_pct = pct
    print(f"  done -> {db_path} ({db_path.stat().st_size / 1024**3:,.2f} GiB)", file=sys.stderr, flush=True)
    return db_path


def table_columns(con, table: str, include_raw_json: bool) -> list[str]:
    rows = con.execute(f'PRAGMA table_info("src.{table}")').fetchall()
    cols = [r[1] for r in rows]
    if not include_raw_json and RAW_JSON_COLUMN in cols:
        cols.remove(RAW_JSON_COLUMN)
    return cols


def import_sqlite_to_duckdb(con, db_path: Path, tables: list[str], include_raw_json: bool) -> dict[str, dict[str, Any]]:
    """Copy each SQLite table into a native DuckDB table in the in-memory catalog.

    Returns a mapping of source-table -> {rows, columns, ts_col, duckdb_table}.
    """
    try:
        con.execute(f"ATTACH '{db_path}' AS src (READONLY)")
    except Exception as exc:
        raise RuntimeError(f"cannot attach SQLite: {exc}") from exc

    src_tables = set()
    for r in con.execute("SHOW ALL TABLES").fetchall():
        catalog, schema, name = r[0], r[1], r[2]
        if catalog == "src" and name != "sqlite_sequence":
            src_tables.add(name)

    imported = {}
    for table in tables:
        if table not in src_tables:
            imported[table] = {"exists": False, "rows": 0, "columns": [], "ts_col": None, "duckdb_table": None}
            continue

        cols = table_columns(con, table, include_raw_json)
        if not cols:
            imported[table] = {"exists": True, "rows": 0, "columns": [], "ts_col": None, "duckdb_table": None}
            continue

        ts_col = TABLE_TIMESTAMP_COLUMNS.get(table)
        duck_table = f"imp_{table}"
        col_list = ", ".join(f'"{c}"' for c in cols)
        t0 = time.perf_counter()
        try:
            con.execute(
                f'CREATE OR REPLACE TABLE memory.main."{duck_table}" AS '
                f'SELECT {col_list} FROM src."{table}"'
            )
            count = con.execute(f'SELECT COUNT(*) FROM memory.main."{duck_table}"').fetchone()[0]
            elapsed = time.perf_counter() - t0
            print(
                f"    imported {table}: {count:,} rows, {len(cols)} cols ({elapsed:.1f}s)",
                file=sys.stderr, flush=True,
            )
            imported[table] = {
                "exists": True,
                "rows": count,
                "columns": cols,
                "ts_col": ts_col if ts_col in cols else None,
                "duckdb_table": duck_table,
            }
        except Exception as exc:
            print(
                f"    imported {table}: FAILED ({type(exc).__name__}: {exc})",
                file=sys.stderr, flush=True,
            )
            imported[table] = {"exists": False, "rows": 0, "columns": [], "ts_col": None, "duckdb_table": None,
                               "error": f"{type(exc).__name__}: {exc}"}

    return imported


def export_table_from_duck(
    con,
    table: str,
    info: dict[str, Any],
    out_dir: Path,
    snapshot_id: str,
    compression: str,
    row_group_size: int,
) -> dict[str, Any]:
    if not info.get("exists") or not info.get("duckdb_table"):
        return {
            "table": table,
            "exists": info.get("exists", False),
            "rows": 0,
            "parts": 0,
            "status": "skipped" if not info.get("duckdb_table") else "empty",
        }

    duck_table = info["duckdb_table"]
    columns = info["columns"]
    ts_col = info["ts_col"]
    select_cols = ", ".join(f'"{c}"' for c in columns)
    full_ref = f'memory.main."{duck_table}"'

    if ts_col:
        date_expr = f'CAST(to_timestamp("{ts_col}" / 1000.0) AS DATE)'
        dates = [
            r[0]
            for r in con.execute(
                f'SELECT DISTINCT {date_expr} AS d FROM {full_ref} '
                f'WHERE "{ts_col}" IS NOT NULL ORDER BY d'
            ).fetchall()
        ]
    else:
        dates = [None]

    total_rows = 0
    parts = 0
    date_counts: dict[str, int] = {}
    t0 = time.perf_counter()
    print(
        f"  {table}: {len(dates) if dates != [None] else 1} partition(s), {info['rows']:,} rows",
        file=sys.stderr, flush=True,
    )

    for i, date in enumerate(dates, 1):
        if date is None:
            where, date_dir, date_key = "", "unpartitioned", "unpartitioned"
        else:
            where = f"WHERE {date_expr} = CAST('{date}' AS DATE)"
            date_dir, date_key = f"date={date}", str(date)

        count = con.execute(
            f'SELECT COUNT(*) FROM {full_ref} {where}'
        ).fetchone()[0]
        if count == 0:
            continue

        date_counts[date_key] = count
        total_rows += count
        parts += 1

        out_path = (
            out_dir / table / date_dir / f"{snapshot_id}-part-{parts:06d}.parquet"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        order = f'ORDER BY "{ts_col}"' if ts_col else ""
        sql = (
            f"COPY (SELECT {select_cols} FROM {full_ref} {where} {order}) "
            f"TO '{out_path}' (FORMAT PARQUET, COMPRESSION '{compression}', "
            f"ROW_GROUP_SIZE {row_group_size})"
        )

        t_part = time.perf_counter()
        try:
            con.execute(sql)
            elapsed = time.perf_counter() - t_part
            size_mb = out_path.stat().st_size / 1024**2
            print(
                f"    [{i}/{len(dates)}] {date_key}: {count:,} rows -> {out_path.name} "
                f"({size_mb:.1f} MiB, {elapsed:.2f}s)",
                file=sys.stderr, flush=True,
            )
        except Exception as exc:
            print(
                f"    [{i}/{len(dates)}] {date_key}: FAILED ({type(exc).__name__}: {exc})",
                file=sys.stderr, flush=True,
            )
            return {
                "table": table, "exists": True, "rows": total_rows,
                "parts": parts, "status": "partial",
                "error": f"{type(exc).__name__}: {exc}",
                "dates": date_counts,
            }

    elapsed = time.perf_counter() - t0
    status = "ok" if total_rows > 0 else "empty"
    print(
        f"  {table}: done {total_rows:,} rows in {parts} file(s) ({elapsed:.1f}s)",
        file=sys.stderr, flush=True,
    )
    return {
        "table": table, "exists": True, "status": status,
        "rows": total_rows, "parts": parts, "dates": date_counts,
    }


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    staging_dir = Path(args.staging_dir)
    out_dir = Path(args.out_dir)

    source, filename = resolve_snapshot(args.snapshot, manifest_path)
    snapshot_id = filename.removesuffix(".db.gz").removesuffix(".db")
    db_path = materialize_db(source, filename, staging_dir)

    con = duckdb.connect(":memory:")
    if args.threads > 0:
        con.execute(f"SET threads = {args.threads}")

    try:
        imported = import_sqlite_to_duckdb(con, db_path, args.tables, args.include_raw_json)
    except RuntimeError as exc:
        print(f"  WARN: DuckDB attach failed ({exc}); falling back to sqlite3", file=sys.stderr, flush=True)
        con.close()
        # Hand off to v1 script
        cmd = [
            ".venv/bin/python",
            "scripts/datasets/export_snapshot_fast.py",
            args.snapshot,
            "--manifest", args.manifest,
            "--staging-dir", args.staging_dir,
            "--out-dir", args.out_dir,
        ]
        import subprocess
        return subprocess.call(cmd)

    print(f"\nExporting partitions from native DuckDB ...", file=sys.stderr, flush=True)
    table_reports = []
    overall_t0 = time.perf_counter()
    for ti, table in enumerate(args.tables, 1):
        print(f"\n[{ti}/{len(args.tables)}] {table}", file=sys.stderr, flush=True)
        report = export_table_from_duck(
            con=con,
            table=table,
            info=imported[table],
            out_dir=out_dir,
            snapshot_id=snapshot_id,
            compression=args.compression,
            row_group_size=args.row_group_size,
        )
        table_reports.append(report)

    con.close()
    overall_elapsed = time.perf_counter() - overall_t0
    total_rows = sum(r["rows"] for r in table_reports)
    total_parts = sum(r["parts"] for r in table_reports)
    print(
        f"\nDONE: {total_rows:,} rows in {total_parts} file(s) across {len(table_reports)} table(s) "
        f"in {overall_elapsed:.1f}s",
        file=sys.stderr, flush=True,
    )

    report = {
        "snapshot": filename,
        "snapshot_id": snapshot_id,
        "source": source,
        "db_path": str(db_path),
        "engine": "duckdb-v2",
        "tables": table_reports,
    }
    report_path = out_dir / "metadata" / f"{snapshot_id}.export_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if not args.keep_db and db_path.parent == staging_dir:
        db_path.unlink(missing_ok=True)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())