#!/usr/bin/env python3
"""Inventory Polymarket BTC SQLite snapshots stored in Bunny Storage.

The script reads the Bunny access key from an environment variable by default
and writes a normalized manifest for downstream archive processing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REGION = "ny"
DEFAULT_STORAGE_ZONE = os.environ.get("BUNNY_STORAGE_ZONE", "YOUR_STORAGE_ZONE")
DEFAULT_STORAGE_FOLDER = "polymarket-bot"
DEFAULT_CDN_BASE = os.environ.get(
    "BUNNY_CDN_BASE",
    f"https://{DEFAULT_STORAGE_ZONE}.b-cdn.net/{DEFAULT_STORAGE_FOLDER}",
)
DEFAULT_OUTPUT_JSON = "data/hf_release/metadata/snapshot_manifest.json"
SNAPSHOT_RE = re.compile(
    r"^polymarket_btc_data_(?P<date>\d{4}-\d{2}-\d{2})_(?P<hms>\d{6})\.db\.gz$"
)


@dataclass(frozen=True)
class Snapshot:
    filename: str
    snapshot_ts: str
    snapshot_ts_ms: int
    compressed_bytes: int
    last_changed: str
    public_url: str
    storage_path: str
    guid: str | None = None
    checksum: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Bunny Storage snapshots and write a normalized manifest."
    )
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--storage-zone", default=DEFAULT_STORAGE_ZONE)
    parser.add_argument("--storage-folder", default=DEFAULT_STORAGE_FOLDER)
    parser.add_argument("--cdn-base", default=DEFAULT_CDN_BASE)
    parser.add_argument("--access-key-env", default="BUNNY_CDN_ACCESS_KEY")
    parser.add_argument(
        "--access-key-command",
        default="",
        help="Optional shell command that prints the access key if the env var is unset.",
    )
    parser.add_argument("--output-json", default=DEFAULT_OUTPUT_JSON)
    parser.add_argument(
        "--output-tsv",
        default="",
        help="Optional TSV output path. Defaults to output-json with .tsv suffix.",
    )
    parser.add_argument(
        "--print-tsv",
        action="store_true",
        help="Also print filename, size, timestamp, and LastChanged to stdout.",
    )
    return parser.parse_args()


def load_access_key(env_name: str, command: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value

    if command:
        completed = subprocess.run(
            command,
            shell=True,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"access key command failed with {completed.returncode}: {completed.stderr.strip()}"
            )
        value = completed.stdout.strip()
        if value:
            return value

    raise RuntimeError(
        f"missing Bunny access key; set {env_name} or pass --access-key-command"
    )


def fetch_storage_listing(url: str, access_key: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers={"AccessKey": access_key})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bunny Storage listing failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Bunny Storage listing failed: {exc}") from exc

    decoded = json.loads(payload)
    if not isinstance(decoded, list):
        raise RuntimeError(f"expected Bunny Storage list response, got {type(decoded).__name__}")
    return decoded


def parse_snapshot_timestamp(filename: str) -> tuple[str, int] | None:
    match = SNAPSHOT_RE.match(filename)
    if not match:
        return None
    raw = f"{match.group('date')} {match.group('hms')}"
    parsed = datetime.strptime(raw, "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), int(parsed.timestamp() * 1000)


def normalize_snapshots(
    objects: list[dict[str, Any]],
    storage_zone: str,
    storage_folder: str,
    cdn_base: str,
) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    clean_cdn_base = cdn_base.rstrip("/")
    clean_folder = storage_folder.strip("/")

    for obj in objects:
        filename = str(obj.get("ObjectName") or "")
        parsed_ts = parse_snapshot_timestamp(filename)
        if parsed_ts is None:
            continue

        length = int(obj.get("Length") or 0)
        last_changed = str(obj.get("LastChanged") or "")
        storage_path = f"{storage_zone}/{clean_folder}/{filename}"
        snapshots.append(
            Snapshot(
                filename=filename,
                snapshot_ts=parsed_ts[0],
                snapshot_ts_ms=parsed_ts[1],
                compressed_bytes=length,
                last_changed=last_changed,
                public_url=f"{clean_cdn_base}/{filename}",
                storage_path=storage_path,
                guid=obj.get("Guid"),
                checksum=obj.get("Checksum"),
            )
        )

    snapshots.sort(key=lambda item: (item.snapshot_ts_ms, item.filename))
    return snapshots


def write_json(path: Path, snapshots: list[Snapshot], listing_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "generated_at": generated_at,
        "listing_url": listing_url,
        "snapshot_count": len(snapshots),
        "total_compressed_bytes": sum(item.compressed_bytes for item in snapshots),
        "snapshots": [asdict(item) for item in snapshots],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_tsv(path: Path, snapshots: list[Snapshot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(
            [
                "filename",
                "snapshot_ts",
                "compressed_bytes",
                "last_changed",
                "public_url",
            ]
        )
        for item in snapshots:
            writer.writerow(
                [
                    item.filename,
                    item.snapshot_ts,
                    item.compressed_bytes,
                    item.last_changed,
                    item.public_url,
                ]
            )


def main() -> int:
    args = parse_args()
    access_key = load_access_key(args.access_key_env, args.access_key_command)
    listing_url = (
        f"https://{args.region}.storage.bunnycdn.com/"
        f"{args.storage_zone}/{args.storage_folder.strip('/')}/"
    )

    objects = fetch_storage_listing(listing_url, access_key)
    snapshots = normalize_snapshots(
        objects=objects,
        storage_zone=args.storage_zone,
        storage_folder=args.storage_folder,
        cdn_base=args.cdn_base,
    )

    output_json = Path(args.output_json)
    output_tsv = Path(args.output_tsv) if args.output_tsv else output_json.with_suffix(".tsv")
    write_json(output_json, snapshots, listing_url)
    write_tsv(output_tsv, snapshots)

    if args.print_tsv:
        for item in snapshots:
            print(
                "\t".join(
                    [
                        item.filename,
                        str(item.compressed_bytes),
                        item.snapshot_ts,
                        item.last_changed,
                    ]
                )
            )

    print(
        f"wrote {len(snapshots)} snapshots to {output_json} and {output_tsv}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
