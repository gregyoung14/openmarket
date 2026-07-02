#!/usr/bin/env python3
"""Wait for ML features batch export, then upload features/ to HF.

Usage:
    .venv/bin/python scripts/datasets/post_ml_features_release.py --wait-pid <pid>
    .venv/bin/python scripts/datasets/post_ml_features_release.py --skip-wait
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
# Deferred: full-archive features upload not planned until large-snapshot
# export is complete. HF currently ships v0.4-features (sample snapshot only).
NEW_VERSION = "v0.5-features"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--skip-wait", action="store_true")
    parser.add_argument("--poll-secs", type=int, default=30)
    parser.add_argument("--new-version", default=NEW_VERSION)
    parser.add_argument("--skip-upload", action="store_true")
    return parser.parse_args()


def wait_pid(pid: int, poll: int) -> None:
    if pid <= 0:
        return
    print(f"waiting for ML export PID {pid} ...", flush=True)
    while subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0:
        time.sleep(poll)
    print(f"PID {pid} finished", flush=True)


def run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    args = parse_args()
    if not args.skip_wait:
        wait_pid(args.wait_pid, args.poll_secs)

    summary = ROOT / "data/hf_release/features_parquet/metadata/ml_features_batch_summary.json"
    if summary.exists():
        print(json.dumps(json.loads(summary.read_text()), indent=2))

    if not args.skip_upload:
        msg = f"upload features/ split across full archive ({args.new_version})"
        if run([PY, "scripts/hf/upload_split.py", "--split", "features",
                "--commit-message", msg]) != 0:
            return 1
        if run([PY, "scripts/hf/bump_dataset_version.py", "--set", args.new_version]) != 0:
            return 1

    out = ROOT / "data/hf_release/features_parquet/metadata/post_ml_features_release.json"
    out.write_text(json.dumps({
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": args.new_version,
        "uploaded": not args.skip_upload,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"post-ML-features release complete -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())