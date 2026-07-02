#!/usr/bin/env python3
"""Export ML feature CSVs from SQLite snapshots or unified Parquet.

Preferred for step3 at scale (unified Parquet, no SQLite):

    cargo build -p step3-parquet-export --release
    .venv/bin/python scripts/ml/export_step3_from_parquet.py

Legacy per-snapshot path (step2_hf + step3 via SQLite):

    .venv/bin/python scripts/datasets/export_ml_features.py \
        --snapshot polymarket_btc_data_2026-03-14_193215.db.gz
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_STAGING = "data/hf_release/staging"
DEFAULT_OUT = Path("data/hf_release/features_parquet")
DEFAULT_EXPORT_DIR = Path("data/hf_release/features_exports")
RECOVER_SCRIPT = "scripts/datasets/recover_snapshot.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        default="polymarket_btc_data_2026-03-14_193215.db.gz",
        help="snapshot filename from the manifest",
    )
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--out-dir", default=DEFAULT_OUT, type=Path)
    parser.add_argument("--export-dir", default=DEFAULT_EXPORT_DIR, type=Path)
    parser.add_argument("--recover", action="store_true",
                        help="attempt sqlite .recover before export")
    parser.add_argument("--skip-export", action="store_true",
                        help="only convert CSVs already in --export-dir")
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--cargo", default="cargo")
    return parser.parse_args()


def snapshot_id(filename: str) -> str:
    return Path(filename).name.removesuffix(".db.gz").removesuffix(".db")


def materialize_db(args: argparse.Namespace) -> Path:
    cmd = [args.python, RECOVER_SCRIPT, args.snapshot, "--manifest", args.manifest,
           "--staging-dir", args.staging_dir]
    if args.recover:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "materialize failed")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    db_line = next((line for line in reversed(lines) if line.endswith(".db")), lines[-1])
    db_path = Path(db_line)
    recovered = db_path.with_name(db_path.stem + ".recovered.db")
    return recovered if recovered.exists() else db_path


def run_ml_export(args: argparse.Namespace, db_path: Path, step: str) -> None:
    env = {
        **os.environ,
        "DATABASE_FILE": str(db_path),
        "ML_EXPORT_DIR": str(args.export_dir),
        "ARCHIVE_EXPORT": "1",
    }
    cmd = [args.cargo, "run", "-p", "market-data-recorder", "--bin", "ml_export", "--release", "--", step]
    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"ml_export {step} failed (rc={result.returncode})")


def ts_column(table_name: str, columns: list[str]) -> str | None:
    if table_name == "step3_binary_calibration":
        return "ts_ms" if "ts_ms" in columns else None
    for candidate in ("bucket_ts_ms", "ts_ms", "source_ts_ms"):
        if candidate in columns:
            return candidate
    return None


def csv_to_parquet(csv_path: Path, out_root: Path, table_name: str) -> dict:
    table = pacsv.read_csv(csv_path)
    columns = table.column_names
    ts_col = ts_column(table_name, columns)
    out_rows = table.num_rows
    parts = 0
    total_bytes = 0

    if ts_col and ts_col in columns:
        ts_values = table[ts_col].to_pylist()
        dates: dict[str, list[int]] = {}
        for idx, ts in enumerate(ts_values):
            if ts is None:
                continue
            day = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            dates.setdefault(day, []).append(idx)
        for day, indices in sorted(dates.items()):
            part = table.take(pa.array(indices, type=pa.int64()))
            out_path = out_root / table_name / f"date={day}" / f"{csv_path.stem}.parquet"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(part, out_path, compression="zstd")
            parts += 1
            total_bytes += out_path.stat().st_size
    else:
        out_path = out_root / table_name / "unpartitioned" / f"{csv_path.stem}.parquet"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, out_path, compression="zstd")
        parts = 1
        total_bytes = out_path.stat().st_size

    return {"table": table_name, "rows": out_rows, "parts": parts, "bytes": total_bytes, "source_csv": str(csv_path)}


def convert_exports(args: argparse.Namespace, snap_id: str) -> dict:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "metadata").mkdir(parents=True, exist_ok=True)
    mappings = {
        "step2_100ms": "step2_features_100ms_",
        "step2_1s": "step2_features_1s_",
        "step3_binary_calibration": "step3_binary_calibration_",
    }
    per_table = []
    for table_name, prefix in mappings.items():
        matches = sorted(args.export_dir.glob(f"{prefix}*.csv"))
        if not matches:
            print(f"WARN: no CSV for {table_name}", file=sys.stderr)
            continue
        csv_path = matches[-1]
        per_table.append(csv_to_parquet(csv_path, args.out_dir, table_name))
        manifest = sorted(args.export_dir.glob(f"{prefix}*.manifest.json"))
        if manifest:
            dest = args.out_dir / "metadata" / manifest[-1].name
            if not dest.exists():
                dest.write_bytes(manifest[-1].read_bytes())

    summary = {
        "snapshot_id": snap_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tables": per_table,
        "total_rows": sum(t["rows"] for t in per_table),
        "total_parts": sum(t["parts"] for t in per_table),
        "total_bytes": sum(t["bytes"] for t in per_table),
    }
    out = args.out_dir / "metadata" / f"{snap_id}.features_export_report.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    args = parse_args()
    snap_id = snapshot_id(args.snapshot)
    args.export_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_export:
        db_path = materialize_db(args)
        print(f"using database: {db_path}", flush=True)
        run_ml_export(args, db_path, "step2_hf")
        run_ml_export(args, db_path, "step3")

    convert_exports(args, snap_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())