#!/usr/bin/env python3
"""Fast SQLite -> partitioned Parquet exporter using DuckDB.

DuckDB can attach a SQLite file directly and stream rows into a Parquet
writer, which is roughly 10-50x faster than iterating sqlite3 rows in
Python. Use this for snapshots >= 1 GB; the older `export_snapshot_to_parquet.py`
remains the fallback for tiny snapshots and environments without DuckDB.

Usage:
    .venv/bin/python scripts/datasets/export_snapshot_fast.py \
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

# Map: parquet partition directory = "date=YYYY-MM-DD"
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

# Tables that have a `raw_json` column we want to skip on the public release
RAW_JSON_COLUMN = "raw_json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", help="filename in manifest, public URL, or local path")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--tables", nargs="+", default=DEFAULT_TABLES)
    parser.add_argument("--include-raw-json", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--row-group-size", type=int, default=100_000)
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
        chunk = 1024 * 1024  # 1 MiB
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
        print(f"downloading {source}", file=sys.stderr)
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
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    cols = [r[1] for r in rows]
    if not include_raw_json and RAW_JSON_COLUMN in cols:
        cols.remove(RAW_JSON_COLUMN)
    return cols


def export_table(
    con,
    table: str,
    out_dir: Path,
    snapshot_id: str,
    include_raw_json: bool,
    compression: str,
    row_group_size: int,
) -> dict[str, Any]:
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except Exception as exc:
        return {
            "table": table, "exists": False, "rows": 0, "parts": 0,
            "status": "error", "error": f"sqlite_master read failed: {exc}",
        }
    if table not in tables:
        return {"table": table, "exists": False, "rows": 0, "parts": 0}

    columns = table_columns(con, table, include_raw_json)
    if not columns:
        return {"table": table, "exists": True, "rows": 0, "parts": 0}

    ts_col = TABLE_TIMESTAMP_COLUMNS.get(table)
    select_cols = ", ".join(f'"{c}"' for c in columns)

    if ts_col and ts_col in columns:
        try:
            dates = [
                r[0]
                for r in con.execute(
                    f'SELECT DISTINCT CAST(to_timestamp("{ts_col}" / 1000.0) AS DATE) AS d '
                    f'FROM "{table}" '
                    f'WHERE "{ts_col}" IS NOT NULL '
                    f'ORDER BY d'
                ).fetchall()
            ]
        except Exception as exc:
            return {
                "table": table, "exists": True, "rows": 0, "parts": 0,
                "status": "error", "error": f"date enumeration failed: {exc}",
            }
    else:
        dates = [None]

    total_rows = 0
    parts = 0
    date_counts: dict[str, int] = {}
    t0 = time.perf_counter()

    print(
        f"  {table}: {len(dates) if dates != [None] else 1} partition(s), columns={len(columns)}",
        file=sys.stderr, flush=True,
    )

    for i, date in enumerate(dates, 1):
        if date is None:
            where = ""
            date_dir = "unpartitioned"
            date_key = "unpartitioned"
        else:
            where = (
                f'WHERE CAST(to_timestamp("{ts_col}" / 1004.0)' if False else
                f'WHERE CAST(to_timestamp("{ts_col}" / 1000.0) AS DATE) = '
                f"CAST('{date}' AS DATE)"
            )
            date_dir = f"date={date}"
            date_key = str(date)

        try:
            count = con.execute(
                f'SELECT COUNT(*) FROM "{table}" {where}'
            ).fetchone()[0]
        except Exception as exc:
            print(f"    [{i}/{len(dates)}] {date_key}: COUNT(*) failed: {exc}", file=sys.stderr, flush=True)
            continue
        if count == 0:
            continue

        date_counts[date_key] = count
        total_rows += count
        parts += 1

        out_path = (
            out_dir / table / date_dir / f"{snapshot_id}-part-{parts:06d}.parquet"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if ts_col and ts_col in columns:
            sql = (
                f"COPY (SELECT {select_cols} FROM \"{table}\" {where} "
                f"ORDER BY \"{ts_col}\") "
                f"TO '{out_path}' (FORMAT PARQUET, COMPRESSION '{compression}', "
                f"ROW_GROUP_SIZE {row_group_size})"
            )
        else:
            sql = (
                f"COPY (SELECT {select_cols} FROM \"{table}\" {where}) "
                f"TO '{out_path}' (FORMAT PARQUET, COMPRESSION '{compression}', "
                f"ROW_GROUP_SIZE {row_group_size})"
            )

        t_part = time.perf_counter()
        try:
            con.execute(sql)
            elapsed = time.perf_counter() - t_part
            size_mb = out_path.stat().st_size / 1024**2 if out_path.exists() else 0
            print(
                f"    [{i}/{len(dates)}] {date_key}: {count:,} rows -> {out_path.name} "
                f"({size_mb:.1f} MiB, {elapsed:.1f}s)",
                file=sys.stderr, flush=True,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - t_part
            print(
                f"    [{i}/{len(dates)}] {date_key}: {count:,} rows FAILED after {elapsed:.1f}s: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr, flush=True,
            )
            return {
                "table": table,
                "exists": True,
                "rows": total_rows,
                "parts": parts,
                "status": "partial",
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
        "table": table,
        "exists": True,
        "status": status,
        "rows": total_rows,
        "parts": parts,
        "dates": date_counts,
    }


def export_table_sqlite3(
    conn,
    table: str,
    out_dir: Path,
    snapshot_id: str,
    include_raw_json: bool,
    compression: str,
    row_group_size: int,
) -> dict[str, Any]:
    """Fallback for corrupt SQLite: read with stdlib sqlite3 per-table.

    Skips tables whose schema or data can't be read; partial OK.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        cols_rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except Exception as exc:
        return {
            "table": table, "exists": False, "rows": 0, "parts": 0,
            "status": "error", "error": f"PRAGMA failed: {exc}",
        }
    if not cols_rows:
        return {"table": table, "exists": False, "rows": 0, "parts": 0}

    columns = [r[1] for r in cols_rows]
    if not include_raw_json and RAW_JSON_COLUMN in columns:
        columns.remove(RAW_JSON_COLUMN)

    ts_col = TABLE_TIMESTAMP_COLUMNS.get(table)
    select_cols = ", ".join(f'"{c}"' for c in columns)

    total_rows = 0
    parts = 0
    date_counts: dict[str, int] = {}
    t0 = time.perf_counter()
    print(
        f"  {table}: columns={len(columns)} (sqlite3 fallback)",
        file=sys.stderr, flush=True,
    )

    try:
        if ts_col and ts_col in columns:
            dates = [
                r[0]
                for r in conn.execute(
                    f'SELECT DISTINCT date({ts_col}/1000, "unixepoch") '
                    f'FROM "{table}" WHERE "{ts_col}" IS NOT NULL ORDER BY 1'
                ).fetchall()
            ]
        else:
            dates = [None]
    except Exception as exc:
        return {
            "table": table, "exists": True, "rows": 0, "parts": 0,
            "status": "error", "error": f"date enumeration failed: {exc}",
        }

    for i, date in enumerate(dates, 1):
        if date is None:
            where, date_dir, date_key = "", "unpartitioned", "unpartitioned"
        else:
            where = f'WHERE date({ts_col}/1000, "unixepoch") = "{date}"'
            date_dir, date_key = f"date={date}", str(date)
        try:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}" {where}').fetchone()[0]
        except Exception as exc:
            print(f"    [{i}/{len(dates)}] {date_key}: COUNT(*) failed: {exc}", file=sys.stderr, flush=True)
            continue
        if count == 0:
            continue

        date_counts[date_key] = count
        total_rows += count
        parts += 1

        out_path = out_dir / table / date_dir / f"{snapshot_id}-part-{parts:06d}.parquet"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        order = f'ORDER BY "{ts_col}"' if ts_col and ts_col in columns else ""
        sql = f'SELECT {select_cols} FROM "{table}" {where} {order}'

        t_part = time.perf_counter()
        try:
            cursor = conn.execute(sql)
            rows = []
            while True:
                try:
                    batch = cursor.fetchmany(50_000)
                except Exception as exc:
                    print(
                        f"    [{i}/{len(dates)}] {date_key}: fetch failed after {total_rows:,} rows: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    return {
                        "table": table, "exists": True, "rows": total_rows,
                        "parts": parts, "status": "partial",
                        "error": f"{type(exc).__name__}: {exc}",
                        "dates": date_counts,
                    }
                if not batch:
                    break
                rows.extend(dict(zip(columns, r)) for r in batch)
            tbl = pa.Table.from_pylist(rows)
            pq.write_table(tbl, out_path, compression=compression)
            elapsed = time.perf_counter() - t_part
            size_mb = out_path.stat().st_size / 1024**2 if out_path.exists() else 0
            print(
                f"    [{i}/{len(dates)}] {date_key}: {count:,} rows -> {out_path.name} "
                f"({size_mb:.1f} MiB, {elapsed:.1f}s)",
                file=sys.stderr, flush=True,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - t_part
            print(
                f"    [{i}/{len(dates)}] {date_key}: FAILED after {elapsed:.1f}s: "
                f"{type(exc).__name__}: {exc}",
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

    print(f"opening {db_path.name} with DuckDB ...", file=sys.stderr, flush=True)
    con = duckdb.connect(":memory:")
    duckdb_ok = False
    try:
        con.execute(f"ATTACH '{db_path}' AS src (READONLY)")
        con.execute("USE src")
        con.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchall()
        duckdb_ok = True
    except Exception as exc:
        print(
            f"  WARN: DuckDB cannot open SQLite (likely corrupt: {exc}); "
            f"will fall back to per-table sqlite3 reads",
            file=sys.stderr, flush=True,
        )
        try:
            con.close()
        except Exception:
            pass
        con = None

    sqlite3_conn = None
    if not duckdb_ok:
        import sqlite3 as _sqlite3
        try:
            sqlite3_conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            print(f"  using per-table sqlite3 fallback", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"FATAL: cannot open SQLite at all: {exc}", file=sys.stderr, flush=True)
            return 2

    table_reports = []
    overall_t0 = time.perf_counter()
    for ti, table in enumerate(args.tables, 1):
        print(f"\n[{ti}/{len(args.tables)}] exporting {table}", file=sys.stderr, flush=True)
        if duckdb_ok:
            report = export_table(
                con=con,
                table=table,
                out_dir=out_dir,
                snapshot_id=snapshot_id,
                include_raw_json=args.include_raw_json,
                compression=args.compression,
                row_group_size=args.row_group_size,
            )
        else:
            report = export_table_sqlite3(
                conn=sqlite3_conn,
                table=table,
                out_dir=out_dir,
                snapshot_id=snapshot_id,
                include_raw_json=args.include_raw_json,
                compression=args.compression,
                row_group_size=args.row_group_size,
            )
        table_reports.append(report)
        print(
            f"  -> {table}: rows={report['rows']:,} parts={report['parts']} "
            f"status={report.get('status', 'ok')}",
            file=sys.stderr, flush=True,
        )

    if con is not None:
        try:
            con.close()
        except Exception:
            pass
    if sqlite3_conn is not None:
        try:
            sqlite3_conn.close()
        except Exception:
            pass
    overall_elapsed = time.perf_counter() - overall_t0
    total_rows = sum(r["rows"] for r in table_reports)
    total_parts = sum(r["parts"] for r in table_reports)
    print(
        f"\nDONE: {total_rows:,} rows in {total_parts} file(s) across {len(table_reports)} table(s) in {overall_elapsed:.1f}s",
        file=sys.stderr, flush=True,
    )

    if con is not None:
        try:
            con.close()
        except Exception:
            pass
    if sqlite3_conn is not None:
        try:
            sqlite3_conn.close()
        except Exception:
            pass

    report = {
        "snapshot": filename,
        "snapshot_id": snapshot_id,
        "source": source,
        "db_path": str(db_path),
        "engine": "duckdb",
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