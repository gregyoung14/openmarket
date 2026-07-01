#!/usr/bin/env python3
"""Export one OpenMarket SQLite snapshot to partitioned Parquet.

The script accepts either a local `.db`, a local `.db.gz`, or a snapshot filename
present in `data/hf_release/metadata/snapshot_manifest.json`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - runtime dependency guard
    pa = None
    pq = None


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a SQLite snapshot to Hugging Face-ready Parquet partitions."
    )
    parser.add_argument(
        "snapshot",
        help="Snapshot filename from manifest, public URL, local .db, or local .db.gz",
    )
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--tables", nargs="+", default=DEFAULT_TABLES)
    parser.add_argument("--chunk-rows", type=int, default=50_000)
    parser.add_argument("--include-raw-json", action="store_true")
    parser.add_argument("--skip-integrity-check", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    return parser.parse_args()


def require_pyarrow() -> None:
    if pa is None or pq is None:
        raise RuntimeError(
            "pyarrow is required. Install with: python3 -m pip install pyarrow"
        )


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"snapshots": []}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_snapshot(snapshot: str, manifest_path: Path) -> tuple[str, str]:
    local = Path(snapshot)
    if local.exists():
        return snapshot, local.name

    if snapshot.startswith("http://") or snapshot.startswith("https://"):
        return snapshot, Path(snapshot).name

    manifest = load_manifest(manifest_path)
    for item in manifest.get("snapshots", []):
        if item.get("filename") == snapshot:
            return item["public_url"], item["filename"]

    raise RuntimeError(f"could not resolve snapshot: {snapshot}")


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp.replace(destination)


def materialize_db(source: str, filename: str, staging_dir: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(source)

    if source_path.exists():
        compressed = source_path
    else:
        compressed = staging_dir / filename
        if not compressed.exists():
            print(f"downloading {source}", file=sys.stderr)
            download(source, compressed)

    if compressed.suffix != ".gz":
        return compressed

    db_name = compressed.name.removesuffix(".gz")
    db_path = staging_dir / db_name
    if db_path.exists():
        return db_path

    print(f"decompressing {compressed} -> {db_path}", file=sys.stderr)
    with gzip.open(compressed, "rb") as src, db_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    return db_path


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "missing"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def columns_for_table(conn: sqlite3.Connection, table: str, include_raw_json: bool) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [str(row[1]) for row in rows]
    if not include_raw_json:
        columns = [column for column in columns if column != "raw_json"]
    return columns


def date_from_ms(value: Any) -> str:
    if value is None:
        return "unknown"
    timestamp = int(value) / 1000.0
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd")


def export_table(
    conn: sqlite3.Connection,
    table: str,
    out_dir: Path,
    snapshot_id: str,
    chunk_rows: int,
    include_raw_json: bool,
) -> dict[str, Any]:
    if not table_exists(conn, table):
        return {"table": table, "exists": False, "rows": 0, "parts": 0}

    timestamp_column = TABLE_TIMESTAMP_COLUMNS.get(table)
    columns = columns_for_table(conn, table, include_raw_json)
    if not columns:
        return {"table": table, "exists": True, "rows": 0, "parts": 0}

    quoted = ", ".join(f'"{column}"' for column in columns)
    order_by = f' ORDER BY "{timestamp_column}"' if timestamp_column in columns else ""
    try:
        cursor = conn.execute(f'SELECT {quoted} FROM "{table}"{order_by}')
    except sqlite3.DatabaseError as exc:
        return {
            "table": table,
            "exists": True,
            "rows": 0,
            "parts": 0,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    total_rows = 0
    part_counter = 0
    date_counts: dict[str, int] = defaultdict(int)
    column_names = [description[0] for description in cursor.description]

    while True:
        try:
            batch = cursor.fetchmany(chunk_rows)
        except sqlite3.DatabaseError as exc:
            return {
                "table": table,
                "exists": True,
                "rows": total_rows,
                "parts": part_counter,
                "status": "partial",
                "error": f"{type(exc).__name__}: {exc}",
                "dates": dict(sorted(date_counts.items())),
            }
        if not batch:
            break

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in batch:
            record = dict(zip(column_names, row))
            if timestamp_column and timestamp_column in record:
                date = date_from_ms(record[timestamp_column])
            else:
                date = "unpartitioned"
            grouped[date].append(record)

        for date, rows in grouped.items():
            part_counter += 1
            total_rows += len(rows)
            date_counts[date] += len(rows)
            path = (
                out_dir
                / table
                / f"date={date}"
                / f"{snapshot_id}-part-{part_counter:06d}.parquet"
            )
            write_rows(path, rows)

    status = "ok"
    if total_rows == 0:
        status = "empty"
    return {
        "table": table,
        "exists": True,
        "status": status,
        "rows": total_rows,
        "parts": part_counter,
        "dates": dict(sorted(date_counts.items())),
    }


def main() -> int:
    args = parse_args()
    require_pyarrow()

    manifest_path = Path(args.manifest)
    staging_dir = Path(args.staging_dir)
    out_dir = Path(args.out_dir)

    source, filename = resolve_snapshot(args.snapshot, manifest_path)
    snapshot_id = filename.removesuffix(".db.gz").removesuffix(".db")
    db_path = materialize_db(source, filename, staging_dir)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = "skipped" if args.skip_integrity_check else integrity_check(conn)
        integrity_status = "ok" if integrity == "ok" else ("skipped" if integrity == "skipped" else "degraded")

        table_reports = []
        for table in args.tables:
            report = export_table(
                conn=conn,
                table=table,
                out_dir=out_dir,
                snapshot_id=snapshot_id,
                chunk_rows=args.chunk_rows,
                include_raw_json=args.include_raw_json,
            )
            table_reports.append(report)
    finally:
        conn.close()

    report = {
        "snapshot": filename,
        "snapshot_id": snapshot_id,
        "source": source,
        "db_path": str(db_path),
        "integrity_check": integrity,
        "integrity_status": integrity_status,
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
