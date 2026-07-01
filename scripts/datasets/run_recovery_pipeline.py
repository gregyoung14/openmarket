#!/usr/bin/env python3
"""Wait for in-flight recovery, then re-export all quarantined snapshots.

Runs recover + parquet re-export sequentially. Cleans prior partial parquet
shards for each snapshot before re-export.

Usage:
    .venv/bin/python scripts/datasets/run_recovery_pipeline.py
    .venv/bin/python scripts/datasets/run_recovery_pipeline.py --wait-pid 97412
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


QUARANTINED = [
    "polymarket_btc_data_2026-04-21_211838",
    "polymarket_btc_data_2026-03-29_215354",
    "polymarket_btc_data_2026-03-22_215354",
    "polymarket_btc_data_2026-05-13_183517",
]

DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_STAGING = "data/hf_release/staging"
DEFAULT_OUT = "data/hf_release/full_parquet"
RECOVER = "scripts/datasets/recover_snapshot.py"
EXPORTER = "scripts/datasets/export_snapshot_v2.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--wait-pid", type=int, default=0,
                        help="optional recover_snapshot.py PID to wait on first")
    parser.add_argument("--poll-secs", type=int, default=15)
    return parser.parse_args()


def manifest_filename(manifest: Path, snapshot_id: str) -> str:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for item in data.get("snapshots", []):
        if item["filename"].removesuffix(".db.gz") == snapshot_id:
            return item["filename"]
    raise KeyError(snapshot_id)


def wait_for_pid(pid: int, poll_secs: int) -> None:
    if pid <= 0:
        return
    print(f"waiting for PID {pid} to finish ...", flush=True)
    while True:
        rc = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode
        if rc != 0:
            print(f"PID {pid} finished", flush=True)
            return
        time.sleep(poll_secs)


def wait_for_journal(recovered: Path, poll_secs: int) -> None:
    journal = Path(f"{recovered}-journal")
    if not journal.exists():
        return
    print(f"waiting for recover journal on {recovered.name} ...", flush=True)
    while journal.exists():
        time.sleep(poll_secs)


def cleanup_snapshot_parquet(out_dir: Path, snapshot_id: str) -> int:
    removed = 0
    for pq in out_dir.rglob(f"*{snapshot_id}*.parquet"):
        pq.unlink()
        removed += 1
    report = out_dir / "metadata" / f"{snapshot_id}.export_report.json"
    if report.exists():
        report.unlink()
        removed += 1
    print(f"cleaned {removed} prior artifact(s) for {snapshot_id}", flush=True)
    return removed


def run_step(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd)


def process_snapshot(args: argparse.Namespace, manifest: Path, snap_id: str) -> dict:
    filename = manifest_filename(manifest, snap_id)
    staging = Path(args.staging_dir)
    out_dir = Path(args.out_dir)
    recovered = staging / f"{snap_id}.recovered.db"

    wait_for_journal(recovered, args.poll_secs)

    while True:
        recover = run_step(
            [args.python, RECOVER, filename,
             "--manifest", args.manifest, "--staging-dir", args.staging_dir],
            Path.cwd(),
        )
        if recover.returncode == 2:
            wait_for_journal(recovered, args.poll_secs)
            continue
        if recover.returncode != 0:
            return {
                "snapshot_id": snap_id,
                "status": "recover_failed",
                "detail": (recover.stderr or recover.stdout or "")[-500:],
            }
        break

    wait_for_journal(recovered, args.poll_secs)
    cleanup_snapshot_parquet(out_dir, snap_id)

    export = run_step(
        [args.python, EXPORTER, filename,
         "--manifest", args.manifest,
         "--out-dir", args.out_dir,
         "--staging-dir", args.staging_dir,
         "--keep-db"],
        Path.cwd(),
    )
    status = "ok" if export.returncode == 0 else "export_failed"
    report_path = out_dir / "metadata" / f"{snap_id}.export_report.json"
    rows = None
    if report_path.exists():
        report = json.loads(report_path.read_text())
        rows = sum(t.get("rows", 0) for t in report.get("tables", []))
    return {
        "snapshot_id": snap_id,
        "status": status,
        "returncode": export.returncode,
        "rows": rows,
    }


def main() -> int:
    args = parse_args()
    manifest = Path(args.manifest)
    wait_for_pid(args.wait_pid, args.poll_secs)

    results = []
    for snap_id in QUARANTINED:
        print(f"\n{'='*60}\n=== {snap_id} ===", flush=True)
        results.append(process_snapshot(args, manifest, snap_id))

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    out = Path(args.out_dir) / "metadata" / "recovery_reexport_summary.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary -> {out}")
    print(json.dumps(summary, indent=2))

    ok = all(r["status"] == "ok" for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())