#!/usr/bin/env python3
"""Produce a redacted copy of the OpenMarket snapshot manifest.

Reads `data/hf_release/metadata/snapshot_manifest.json`, writes
`data/hf_release/metadata_redacted/snapshot_manifest.{json,tsv}` with:

- `storage_path` field stripped (it referenced the operator's CDN storage zone)
- `public_url` rewritten to replace the storage-zone hostname with a placeholder
  (`cdn.example.com/<zone>`) while preserving the file path
- `checksum`, `filename`, `compressed_bytes`, `last_changed`, `snapshot_ts`,
  `snapshot_ts_ms`, `guid` all preserved

The original manifest is not modified.

Usage:
    .venv/bin/python scripts/hf/redact_manifest.py
    .venv/bin/python scripts/hf/redact_manifest.py --manifest <path> --out <path>
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = "data/hf_release/metadata/snapshot_manifest.json"
DEFAULT_OUT_DIR = "data/hf_release/metadata_redacted"
DEFAULT_PLACEHOLDER_HOST = "cdn.example.com"

# Storage-zone name in the operator's Bunny CDN bucket. Detected at runtime
# from the manifest itself, so we don't hardcode it.
URL_FIELD = "public_url"
PATH_FIELD = "storage_path"
PRESERVE_FIELDS = (
    "filename",
    "checksum",
    "compressed_bytes",
    "snapshot_ts",
    "snapshot_ts_ms",
    "last_changed",
    "guid",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--placeholder-host", default=DEFAULT_PLACEHOLDER_HOST)
    parser.add_argument(
        "--drop-fields",
        nargs="+",
        default=[PATH_FIELD],
        help="fields to strip from each snapshot entry",
    )
    return parser.parse_args()


def detect_storage_zone(manifest: dict[str, Any]) -> str:
    """Return the full CDN hostname to rewrite (e.g. `glitchrun-xyz.b-cdn.net`).

    Prefers the URL host so the regex matches the full domain, not just the
    storage-zone prefix.
    """
    for snap in manifest.get("snapshots", []):
        url = snap.get(URL_FIELD, "")
        m = re.match(r"https?://([^/]+)/", url)
        if m:
            return m.group(1)
    raise RuntimeError(f"could not detect storage zone from manifest {manifest.get('source')}")


def rewrite_url(url: str, zone: str, placeholder_host: str) -> str:
    return re.sub(
        rf"https?://{re.escape(zone)}/",
        f"https://{placeholder_host}/",
        url,
    )


def redact_entry(entry: dict[str, Any], zone: str, placeholder_host: str, drop_fields: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, value in entry.items():
        if field in drop_fields:
            continue
        if field == URL_FIELD:
            value = rewrite_url(value, zone, placeholder_host)
        out[field] = value
    return out


def main() -> int:
    args = parse_args()
    src = Path(args.manifest)
    if not src.exists():
        raise SystemExit(f"manifest not found: {src}")

    manifest = json.loads(src.read_text(encoding="utf-8"))
    zone = detect_storage_zone(manifest)
    print(f"detected storage zone: {zone}")

    redacted_snapshots = [
        redact_entry(s, zone, args.placeholder_host, args.drop_fields)
        for s in manifest.get("snapshots", [])
    ]
    redacted = {
        "snapshots": redacted_snapshots,
        "total_compressed_bytes": manifest.get("total_compressed_bytes"),
        "redacted": True,
        "redacted_from": str(src),
        "dropped_fields": args.drop_fields,
        "placeholder_host": args.placeholder_host,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "snapshot_manifest.json"
    json_path.write_text(json.dumps(redacted, indent=2) + "\n", encoding="utf-8")

    tsv_path = out_dir / "snapshot_manifest.tsv"
    if redacted_snapshots:
        fieldnames = [f for f in PRESERVE_FIELDS if f in redacted_snapshots[0]]
        with tsv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            for entry in redacted_snapshots:
                writer.writerow({f: entry.get(f, "") for f in fieldnames})

    print(f"wrote {json_path}")
    print(f"wrote {tsv_path}")
    print(f"snapshots: {len(redacted_snapshots)}")
    print(f"total bytes: {manifest.get('total_compressed_bytes'):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())