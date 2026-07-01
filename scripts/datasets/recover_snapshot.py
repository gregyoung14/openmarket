#!/usr/bin/env python3
"""Recover a corrupt SQLite snapshot using `sqlite3 .recover`.

Downloads/decompresses a manifest snapshot if needed, runs integrity_check,
and when the image is corrupt produces `<snapshot>.recovered.db`.

Usage:
    .venv/bin/python scripts/datasets/recover_snapshot.py \
        polymarket_btc_data_2026-04-21_211838.db.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_STAGING = "data/hf_release/staging"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", help="snapshot filename or snapshot_id")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def resolve_snapshot(snapshot: str, manifest_path: Path) -> tuple[str, str]:
    if snapshot.startswith(("http://", "https://")):
        return snapshot, Path(snapshot).name
    if Path(snapshot).exists():
        return snapshot, Path(snapshot).name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("snapshots", []):
        if item.get("filename") == snapshot or item.get("filename", "").removesuffix(".db.gz") == snapshot:
            return item["public_url"], item["filename"]
    raise RuntimeError(f"could not resolve snapshot: {snapshot}")


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp.replace(destination)


def materialize(source: str, filename: str, staging: Path) -> Path:
    staging.mkdir(parents=True, exist_ok=True)
    src = Path(source)
    compressed = src if src.exists() else staging / filename
    if not compressed.exists():
        print(f"downloading {source}", file=sys.stderr, flush=True)
        download(source, compressed)
    if compressed.suffix != ".gz":
        return compressed
    db_path = staging / compressed.name.removesuffix(".gz")
    if db_path.exists():
        return db_path
    print(f"decompressing {compressed.name}", file=sys.stderr, flush=True)
    with gzip.open(compressed, "rb") as src_f, db_path.open("wb") as dst:
        shutil.copyfileobj(src_f, dst)
    return db_path


def integrity_ok(db_path: Path) -> bool:
    result = subprocess.run(
        ["sqlite3", str(db_path), "PRAGMA integrity_check;"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "").strip()
    print(f"integrity_check: {output.splitlines()[0] if output else 'empty'}", flush=True)
    return output == "ok"


def recover_db(db_path: Path) -> Path:
    recovered = db_path.with_name(db_path.stem + ".recovered.db")
    recovered.unlink(missing_ok=True)
    print(f"running sqlite3 .recover -> {recovered.name}", flush=True)
    dump = subprocess.Popen(
        ["sqlite3", str(db_path), ".recover"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        load = subprocess.run(
            ["sqlite3", str(recovered)],
            stdin=dump.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if dump.stdout:
            dump.stdout.close()
        dump.wait()
    if dump.returncode != 0:
        raise RuntimeError(dump.stderr or "sqlite3 .recover failed")
    if load.returncode != 0:
        raise RuntimeError(load.stderr or load.stdout or "sqlite3 load of recovered SQL failed")
    if not recovered.exists() or recovered.stat().st_size == 0:
        raise RuntimeError(f"recovered database missing or empty: {recovered}")
    if not integrity_ok(recovered):
        print("WARN: recovered DB still fails integrity_check", file=sys.stderr, flush=True)
    return recovered


def main() -> int:
    args = parse_args()
    source, filename = resolve_snapshot(args.snapshot, Path(args.manifest))
    snapshot_id = filename.removesuffix(".db.gz").removesuffix(".db")
    db_path = materialize(source, filename, Path(args.staging_dir))
    recovered_path = db_path.with_name(db_path.stem + ".recovered.db")
    recovered_journal = Path(f"{recovered_path}-journal")

    if recovered_journal.exists():
        print(f"recover in progress: {recovered_path}", flush=True)
        return 2

    if recovered_path.exists() and recovered_path.stat().st_size > 0 and not args.force:
        print(recovered_path)
        return 0

    if integrity_ok(db_path):
        print(db_path)
        return 0

    recovered = recover_db(db_path)
    print(recovered)
    report = {
        "snapshot_id": snapshot_id,
        "source_db": str(db_path),
        "recovered_db": str(recovered),
        "source_bytes": db_path.stat().st_size,
        "recovered_bytes": recovered.stat().st_size,
    }
    out = Path(args.staging_dir) / f"{snapshot_id}.recovery_report.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())