#!/usr/bin/env python3
"""Re-export the four quarantined partial/corrupt snapshots.

Attempts sqlite `.recover` when needed, then re-runs the v2 parquet exporter
with --force.

Usage:
    .venv/bin/python scripts/datasets/reexport_corrupt_snapshots.py --list-only
    .venv/bin/python scripts/datasets/reexport_corrupt_snapshots.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


QUARANTINED = [
    "polymarket_btc_data_2026-03-29_215354",
    "polymarket_btc_data_2026-03-22_215354",
    "polymarket_btc_data_2026-05-13_183517",
    "polymarket_btc_data_2026-04-21_211838",
]

DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_STAGING = "data/hf_release/staging"
DEFAULT_OUT = "data/hf_release/full_parquet"
EXPORTER = "scripts/datasets/export_snapshot_v2.py"
RECOVER = "scripts/datasets/recover_snapshot.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--snapshot", action="append", default=[],
                        help="subset of quarantined snapshot ids")
    return parser.parse_args()


def manifest_filename(manifest: Path, snapshot_id: str) -> str:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for item in data.get("snapshots", []):
        if item["filename"].removesuffix(".db.gz") == snapshot_id:
            return item["filename"]
    raise KeyError(snapshot_id)


def main() -> int:
    args = parse_args()
    targets = args.snapshot or QUARANTINED
    manifest = Path(args.manifest)

    if args.list_only:
        for snap in targets:
            print(snap)
        return 0

    results = []
    for snap_id in targets:
        filename = manifest_filename(manifest, snap_id.removesuffix(".db.gz"))
        print(f"\n=== recover {snap_id} ===", flush=True)
        recover = subprocess.run(
            [args.python, RECOVER, filename, "--manifest", args.manifest,
             "--staging-dir", args.staging_dir, "--force"],
            capture_output=True,
            text=True,
        )
        if recover.returncode != 0:
            results.append({
                "snapshot_id": snap_id,
                "status": "recover_failed",
                "stderr": (recover.stderr or recover.stdout or "").strip()[-500:],
            })
            continue

        db_line = next(
            (line.strip() for line in reversed((recover.stdout or "").splitlines())
             if line.strip().endswith(".db")),
            None,
        )
        export_target = filename
        if db_line and db_line.endswith(".recovered.db"):
            export_target = Path(db_line).name.replace(".recovered.db", ".db.gz")

        print(f"=== export {snap_id} ===", flush=True)
        export = subprocess.run(
            [args.python, EXPORTER, export_target,
             "--manifest", args.manifest,
             "--out-dir", args.out_dir,
             "--staging-dir", args.staging_dir,
             "--keep-db"],
        )
        results.append({
            "snapshot_id": snap_id,
            "status": "ok" if export.returncode == 0 else "export_failed",
            "returncode": export.returncode,
        })

    out = Path(args.out_dir) / "metadata" / "recovery_reexport_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"results": results}, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary -> {out}")
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())