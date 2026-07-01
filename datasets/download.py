#!/usr/bin/env python3
"""Download OpenMarket dataset artifacts.

This script is intentionally small and dependency-light. The public release path
is Hugging Face Datasets; the legacy CDN path remains supported for migration.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import urllib.request
from pathlib import Path


LEGACY_SNAPSHOTS = {
    "sample": "https://glitchrun-xyz.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz",
    "2026-03-14_193215": "https://glitchrun-xyz.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz",
}


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp.replace(destination)


def gunzip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(source, "rb") as compressed, destination.open("wb") as out:
        shutil.copyfileobj(compressed, out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="sample", help="snapshot name or URL")
    parser.add_argument("--out", default="data/openmarket.db", help="output DB path")
    parser.add_argument("--keep-compressed", action="store_true")
    args = parser.parse_args()

    url = LEGACY_SNAPSHOTS.get(args.snapshot, args.snapshot)
    out = Path(args.out)
    compressed = out.with_suffix(out.suffix + ".gz")

    print(f"Downloading {url}", file=sys.stderr)
    download(url, compressed)

    print(f"Decompressing to {out}", file=sys.stderr)
    gunzip(compressed, out)

    if not args.keep_compressed:
        compressed.unlink()

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
