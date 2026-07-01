#!/usr/bin/env python3
"""Export multiple OpenMarket SQLite snapshots to a unified Parquet split.

Resolves snapshot filenames through the manifest, runs the per-snapshot
exporter for each, and aggregates per-snapshot reports into a single
multi-export report. Skips snapshots that already have an export report
unless --force is passed.

The operator-ready path is queue-driven:

- `clean`: manifest snapshots not yet published and not held in `corrupt`
- `corrupt`: explicit hold queue for snapshots that failed clean export
- `published-clean` / `published-partial`: snapshots already released on HF

Usage:
    .venv/bin/python scripts/datasets/export_many_snapshots.py \
        --manifest <OPENMARKET_REPO>/data/hf_release/metadata/snapshot_manifest.json \
        --reports-dir <OPENMARKET_REPO>/data/hf_release/full_parquet/metadata \
        --status-file docs/release/full-snapshot-publish-status.json \
        --queue clean \
        --min-bytes 0 \
        --batch-size 10 \
        --batch-index 1
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_OUT_DIR = "data/hf_release/full_parquet"
DEFAULT_STAGING = "data/hf_release/staging"
DEFAULT_STATUS_FILE = "docs/release/full-snapshot-publish-status.json"
EXPORTER_V2 = "scripts/datasets/export_snapshot_v2.py"
EXPORTER_V1 = "scripts/datasets/export_snapshot_fast.py"
EXPORTER_LEGACY = "scripts/datasets/export_snapshot_to_parquet.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--status-file", default=DEFAULT_STATUS_FILE,
                        help="Queue-state JSON for clean/published/corrupt separation")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--reports-dir", type=Path,
                        help="Metadata directory used to detect already-exported snapshots; defaults to <out-dir>/metadata")
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--max-snapshots", type=int, default=None,
                        help="Backward-compatible alias for --batch-size")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Maximum snapshots to process from the selected queue")
    parser.add_argument("--batch-index", type=int, default=1,
                        help="1-based batch index within the selected queue")
    parser.add_argument("--queue",
                        choices=("clean", "corrupt", "published-clean", "published-partial", "all"),
                        default="clean")
    parser.add_argument("--lane", choices=("publish", "recovery", "all"), default=None,
                        help="Deprecated alias: publish=clean, recovery=corrupt, all=all")
    parser.add_argument("--snapshot-ids-file", type=Path,
                        help="Exact snapshot ids to export, one per line or as JSON")
    parser.add_argument("--write-plan", type=Path,
                        help="Optional JSON output describing queue selection and the chosen batch")
    parser.add_argument("--list-only", action="store_true",
                        help="Print the selected batch without exporting anything")
    parser.add_argument("--min-bytes", type=int, default=10 * 1024 * 1024,
                        help="Only export snapshots at least this large (skip tiny residue)")
    parser.add_argument("--engine", choices=("v2", "v1", "auto"), default="auto",
                        help="Exporter to use: v2 (DuckDB native, fastest), v1 (DuckDB attached), auto")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--chunk-rows", type=int, default=50_000,
                        help="deprecated no-op; kept for compatibility with release_split.py")
    return parser.parse_args()


def existing_reports(meta: Path) -> set[str]:
    if not meta.exists():
        return set()
    return {p.stem.replace(".export_report", "") for p in meta.glob("*.export_report.json")}


def normalize_snapshot_id(value: str) -> str:
    name = Path(value).name
    return name.removesuffix(".db.gz").removesuffix(".db")


def load_status_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"queues": {}, "notes": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"status file must be a JSON object: {path}")
    data.setdefault("queues", {})
    data.setdefault("notes", {})
    return data


def queue_snapshot_ids(status: dict[str, Any], queue_name: str) -> set[str]:
    queue = status.get("queues", {}).get(queue_name, [])
    snapshot_ids: set[str] = set()
    for item in queue:
        if isinstance(item, dict):
            snapshot_id = item.get("snapshot_id") or item.get("id") or item.get("snapshot")
            if snapshot_id:
                snapshot_ids.add(normalize_snapshot_id(snapshot_id))
        elif isinstance(item, str):
            snapshot_ids.add(normalize_snapshot_id(item))
    return snapshot_ids


def classify_snapshot(snapshot_id: str, status: dict[str, Any]) -> str:
    for queue_name in ("published-clean", "published-partial", "corrupt"):
        if snapshot_id in queue_snapshot_ids(status, queue_name):
            return queue_name
    return "clean"


def load_snapshot_ids_file(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if path.suffix == ".json":
        data = json.loads(raw)
        if isinstance(data, dict):
            items = data.get("selected") or data.get("snapshots") or []
            result = []
            for item in items:
                if isinstance(item, dict):
                    value = item.get("snapshot_id") or item.get("filename") or item.get("snapshot")
                    if value:
                        result.append(normalize_snapshot_id(value))
                elif isinstance(item, str):
                    result.append(normalize_snapshot_id(item))
            return result
        if isinstance(data, list):
            return [normalize_snapshot_id(str(item)) for item in data]
        raise RuntimeError(f"unsupported JSON snapshot id payload: {path}")
    return [
        normalize_snapshot_id(line)
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def build_candidates(
    snapshots: list[dict[str, Any]],
    status: dict[str, Any],
    queue: str,
    min_bytes: int,
) -> list[dict[str, Any]]:
    candidates = []
    for snapshot in snapshots:
        snapshot_id = normalize_snapshot_id(snapshot["filename"])
        snapshot_queue = classify_snapshot(snapshot_id, status)
        item = dict(snapshot)
        item["snapshot_id"] = snapshot_id
        item["queue"] = snapshot_queue
        if queue == "all":
            if snapshot["compressed_bytes"] >= min_bytes:
                candidates.append(item)
            continue
        if queue == "clean":
            if snapshot_queue == "clean" and snapshot["compressed_bytes"] >= min_bytes:
                candidates.append(item)
            continue
        if snapshot_queue == queue:
            candidates.append(item)
    return sorted(
        candidates,
        key=lambda s: (s.get("compressed_bytes", 0), s["snapshot_id"]),
        reverse=True,
    )


def write_plan(
    destination: Path,
    *,
    args: argparse.Namespace,
    snapshots: list[dict[str, Any]],
    queue_counts: dict[str, int],
    selected: list[dict[str, Any]],
    done: set[str],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue": args.queue,
        "batch_index": args.batch_index,
        "batch_size": args.batch_size,
        "min_bytes": args.min_bytes,
        "manifest_snapshots": len(snapshots),
        "queue_counts": queue_counts,
        "already_exported_reports": sorted(done),
        "selected": [
            {
                "snapshot_id": item["snapshot_id"],
                "filename": item["filename"],
                "compressed_bytes": item["compressed_bytes"],
                "queue": item["queue"],
            }
            for item in selected
        ],
    }
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.lane:
        args.queue = {"publish": "clean", "recovery": "corrupt", "all": "all"}[args.lane]
    if args.max_snapshots is not None:
        args.batch_size = args.max_snapshots
    if args.batch_index < 1:
        raise SystemExit("--batch-index must be >= 1")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")

    manifest = json.loads(Path(args.manifest).read_text())
    snapshots = manifest["snapshots"]
    status = load_status_file(Path(args.status_file))
    queue_counts = {
        name: len(build_candidates(snapshots, status, name, 0))
        for name in ("clean", "corrupt", "published-clean", "published-partial")
    }
    candidates = build_candidates(snapshots, status, args.queue, args.min_bytes)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = args.reports_dir or (out_dir / "metadata")
    done = existing_reports(reports_dir) if not args.force else set()

    if args.snapshot_ids_file:
        requested_ids = load_snapshot_ids_file(args.snapshot_ids_file)
        by_id = {item["snapshot_id"]: item for item in candidates}
        missing = [snapshot_id for snapshot_id in requested_ids if snapshot_id not in by_id]
        if missing:
            missing_text = ", ".join(missing)
            raise SystemExit(f"snapshot ids not in {args.queue} queue: {missing_text}")
        ordered = [by_id[snapshot_id] for snapshot_id in requested_ids]
    else:
        batch_start = (args.batch_index - 1) * args.batch_size
        batch_stop = batch_start + args.batch_size
        ordered = candidates[batch_start:batch_stop]

    selected = [s for s in ordered if s["snapshot_id"] not in done]
    print(f"manifest snapshots: {len(snapshots)}")
    print(f"queue: {args.queue}")
    print(f"batch: {args.batch_index} (size {args.batch_size})")
    print(f"clean queue: {queue_counts['clean']}")
    print(f"corrupt queue: {queue_counts['corrupt']}")
    print(f"published clean: {queue_counts['published-clean']}")
    print(f"published partial: {queue_counts['published-partial']}")
    print(f"candidates (>= {args.min_bytes:,} bytes where applicable): {len(candidates)}")
    print(f"already exported (skipping): {len(done)}")
    print(f"selected for this batch: {len(ordered)}")
    print(f"will export: {len(selected)}")

    if args.write_plan:
        write_plan(
            args.write_plan,
            args=args,
            snapshots=snapshots,
            queue_counts=queue_counts,
            selected=ordered,
            done=done,
        )
        print(f"plan -> {args.write_plan}")

    if args.list_only:
        for snap in ordered:
            print(f"{snap['snapshot_id']}\t{snap['compressed_bytes']}\t{snap['queue']}\t{snap['filename']}")
        return 0

    for snap in selected:
        print(f"\n=== {snap['filename']} ({snap['compressed_bytes']:,} bytes) ===", flush=True)
        if args.engine == "v2":
            exporter = EXPORTER_V2
        elif args.engine == "v1":
            exporter = EXPORTER_V1
        else:
            exporter = EXPORTER_V2  # auto: always use v2
        print(f"  using {exporter}", flush=True)
        cmd = [
            args.python, exporter, snap["filename"],
            "--manifest", args.manifest,
            "--out-dir", args.out_dir,
            "--staging-dir", args.staging_dir,
        ]
        if args.keep_db:
            cmd.append("--keep-db")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"FAILED: {snap['filename']} (rc={result.returncode})", file=sys.stderr, flush=True)
            continue

    agg_path = out_dir / "metadata" / "full_export_summary.json"
    summary = {
        "manifest_snapshots": len(snapshots),
        "candidates": len(candidates),
        "queue": args.queue,
        "batch_index": args.batch_index,
        "batch_size": args.batch_size,
        "reports_dir": str(reports_dir),
        "selected_snapshot_ids": [snap["snapshot_id"] for snap in ordered],
        "exported_reports": sorted(existing_reports(out_dir / "metadata")),
    }
    agg_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary -> {agg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
