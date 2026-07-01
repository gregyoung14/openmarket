#!/usr/bin/env python3
"""Wait for recovery pipeline, then re-merge unified/ and upload to HF.

Usage:
    .venv/bin/python scripts/datasets/post_recovery_unified_release.py
    .venv/bin/python scripts/datasets/post_recovery_unified_release.py --wait-pid 1137
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = ".venv/bin/python"
NEW_VERSION = "v0.5-unified"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--poll-secs", type=int, default=30)
    parser.add_argument("--new-version", default=NEW_VERSION)
    parser.add_argument("--skip-upload", action="store_true")
    return parser.parse_args()


def wait_pid(pid: int, poll: int) -> None:
    if pid <= 0:
        return
    print(f"waiting for recovery pipeline PID {pid} ...", flush=True)
    while subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0:
        time.sleep(poll)
    print(f"PID {pid} finished", flush=True)


def run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    args = parse_args()
    wait_pid(args.wait_pid, args.poll_secs)

    # If no explicit PID, wait for run_recovery_pipeline.py processes.
    if args.wait_pid <= 0:
        print("waiting for any run_recovery_pipeline.py to finish ...", flush=True)
        while True:
            rc = subprocess.run(
                ["pgrep", "-f", "run_recovery_pipeline.py"],
                capture_output=True,
            ).returncode
            if rc != 0:
                break
            time.sleep(args.poll_secs)
        print("recovery pipeline idle", flush=True)

    steps = [
        [PY, "scripts/datasets/merge_partitions.py", "--force"],
        [PY, "scripts/datasets/aggregate_export_reports.py", "--split", "unified"],
    ]
    for cmd in steps:
        if run(cmd) != 0:
            print(f"FAILED: {' '.join(cmd)}", file=sys.stderr)
            return 1

    quality = ROOT / "data/hf_release/unified_parquet/metadata/merge_quality_report.json"
    if quality.exists():
        print(json.dumps(json.loads(quality.read_text()), indent=2))

    if not args.skip_upload:
        if run([
            PY, "scripts/hf/upload_split.py", "--split", "unified",
            "--commit-message",
            f"rebuild unified/ after corrupt snapshot recovery ({args.new_version})",
        ]) != 0:
            return 1
        if run([PY, "scripts/hf/bump_dataset_version.py", "--set", args.new_version]) != 0:
            return 1
        from huggingface_hub import HfApi
        card = ROOT / "datasets/hf/README.md"
        HfApi().upload_file(
            path_or_fileobj=str(card),
            path_in_repo="README.md",
            repo_id="gregyoung14/openmarket-btc-polymarket",
            repo_type="dataset",
            commit_message=f"Bump dataset card to {args.new_version}",
        )

    summary = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": args.new_version,
        "uploaded": not args.skip_upload,
    }
    out = ROOT / "data/hf_release/unified_parquet/metadata/post_recovery_release.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\npost-recovery release complete -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())