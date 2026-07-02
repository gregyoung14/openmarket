#!/usr/bin/env python3
"""Export step3 binary calibration CSV from unified Parquet (Rust backend).

Wraps the `export_step3_from_parquet` binary for Python/Makefile workflows.

Usage:
    cargo build -p step3-parquet-export --release
    .venv/bin/python scripts/ml/export_step3_from_parquet.py
    .venv/bin/python scripts/ml/export_step3_from_parquet.py --market-limit 100
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO / "data/hf_release/unified_parquet"
DEFAULT_OUT = REPO / "data/hf_release/features_exports"
BINARY = REPO / "target/release/export_step3_from_parquet"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step3 export from unified Parquet")
    p.add_argument("--parquet-root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--start-ts-ms", type=int, default=None)
    p.add_argument("--end-ts-ms", type=int, default=None)
    p.add_argument("--market-limit", type=int, default=None)
    p.add_argument("--build", action="store_true", help="cargo build --release first")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.build or not BINARY.exists():
        print("building export_step3_from_parquet...", flush=True)
        subprocess.run(
            ["cargo", "build", "-p", "step3-parquet-export", "--release"],
            cwd=REPO,
            check=True,
        )

    cmd = [
        str(BINARY),
        "--parquet-root",
        str(args.parquet_root),
        "--out-dir",
        str(args.out_dir),
    ]
    if args.start_ts_ms is not None:
        cmd.extend(["--start-ts-ms", str(args.start_ts_ms)])
    if args.end_ts_ms is not None:
        cmd.extend(["--end-ts-ms", str(args.end_ts_ms)])
    if args.market_limit is not None:
        cmd.extend(["--market-limit", str(args.market_limit)])

    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=REPO, text=True)
    if proc.returncode != 0:
        return proc.returncode

    manifests = sorted(args.out_dir.glob("step3_binary_calibration_*.manifest.json"))
    if manifests:
        summary = json.loads(manifests[-1].read_text(encoding="utf-8"))
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())