#!/usr/bin/env python3
"""Export ML features for many archived snapshots into features_parquet/.

Runs export_ml_features.py per snapshot, skipping snapshots that already have a
features export report unless --force is passed.

Usage:
    .venv/bin/python scripts/datasets/export_many_ml_features.py
    .venv/bin/python scripts/datasets/export_many_ml_features.py --list-only
    .venv/bin/python scripts/datasets/export_many_ml_features.py --max-snapshots 5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_OUT = "data/hf_release/features_parquet"
EXPORTER = "scripts/datasets/export_ml_features.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--max-snapshots", type=int, default=None)
    parser.add_argument("--min-bytes", type=int, default=0,
                        help="skip snapshots smaller than this compressed size")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--recover", action="store_true",
                        help="pass --recover to per-snapshot exporter")
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--list-only", action="store_true")
    return parser.parse_args()


def snapshot_id(filename: str) -> str:
    return Path(filename).name.removesuffix(".db.gz").removesuffix(".db")


def report_path(out_dir: Path, snap_id: str) -> Path:
    return out_dir / "metadata" / f"{snap_id}.features_export_report.json"


def load_manifest(manifest: Path) -> list[dict]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    # Small snapshots first so batch progress (and failures) surface early.
    return sorted(
        data.get("snapshots", []),
        key=lambda s: (s.get("compressed_bytes", 0), s["filename"]),
    )


def select_snapshots(args: argparse.Namespace) -> list[dict]:
    out_dir = Path(args.out_dir)
    selected = []
    for item in load_manifest(Path(args.manifest)):
        if item.get("compressed_bytes", 0) < args.min_bytes:
            continue
        snap_id = snapshot_id(item["filename"])
        if report_path(out_dir, snap_id).exists() and not args.force:
            continue
        selected.append(item)
        if args.max_snapshots and len(selected) >= args.max_snapshots:
            break
    return selected


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata").mkdir(parents=True, exist_ok=True)

    batch = select_snapshots(args)
    if args.list_only:
        for item in batch:
            print(item["filename"])
        print(f"total: {len(batch)}", flush=True)
        return 0

    results = []
    for idx, item in enumerate(batch, start=1):
        snap_id = snapshot_id(item["filename"])
        print(f"\n{'=' * 60}", flush=True)
        print(f"[{idx}/{len(batch)}] {snap_id}", flush=True)
        cmd = [args.python, EXPORTER, "--snapshot", item["filename"],
               "--out-dir", str(out_dir)]
        if args.recover:
            cmd.append("--recover")
        print(f"$ {' '.join(cmd)}", flush=True)
        rc = subprocess.call(cmd)
        report = report_path(out_dir, snap_id)
        rows = 0
        if report.exists():
            rows = json.loads(report.read_text(encoding="utf-8")).get("total_rows", 0)
        results.append({
            "snapshot_id": snap_id,
            "filename": item["filename"],
            "status": "ok" if rc == 0 else "failed",
            "returncode": rc,
            "rows": rows,
        })
        if rc != 0:
            print(f"FAILED: {snap_id} (rc={rc})", file=sys.stderr, flush=True)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "processed": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "total_rows": sum(r["rows"] for r in results),
        "results": results,
    }
    out = out_dir / "metadata" / "ml_features_batch_summary.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary -> {out}", flush=True)
    print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())