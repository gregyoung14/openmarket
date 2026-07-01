#!/usr/bin/env python3
"""Download OpenMarket dataset artifacts from Hugging Face.

The canonical public path is the HF dataset repo. Legacy Bunny CDN SQLite
snapshots remain available via `--legacy-cdn` for operator migration only.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path


HF_REPO = "gregyoung14/openmarket-btc-polymarket"
HF_SPLITS = ("sample", "unified", "full")

LEGACY_SNAPSHOTS = {
    "sample": "https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz",
    "2026-03-14_193215": "https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz",
}


def download_url(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp.replace(destination)


def gunzip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(source, "rb") as compressed, destination.open("wb") as out:
        shutil.copyfileobj(compressed, out)


def download_hf_split(split: str, out_dir: Path) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit(
            "huggingface_hub is required for HF downloads. "
            "Install with: pip install huggingface_hub"
        ) from exc

    if split not in HF_SPLITS:
        raise SystemExit(f"unknown HF split {split!r}; choose from {HF_SPLITS}")

    patterns = [f"{split}/**", "metadata/**", "README.md"]
    if split == "sample":
        patterns.append("*.parquet")

    root = Path(snapshot_download(
        HF_REPO,
        repo_type="dataset",
        allow_patterns=patterns,
        local_dir=out_dir,
    ))
    print(f"downloaded {split}/ split to {root}", file=sys.stderr)
    return root


def download_legacy_cdn(snapshot: str, out: Path, keep_compressed: bool) -> Path:
    url = LEGACY_SNAPSHOTS.get(snapshot, snapshot)
    compressed = out.with_suffix(out.suffix + ".gz")
    print(f"Downloading legacy CDN snapshot: {url}", file=sys.stderr)
    download_url(url, compressed)
    print(f"Decompressing to {out}", file=sys.stderr)
    gunzip(compressed, out)
    if not keep_compressed:
        compressed.unlink()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--split",
        default="unified",
        choices=HF_SPLITS,
        help="HF dataset split to download (default: unified)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output directory (HF) or SQLite path (legacy CDN)",
    )
    parser.add_argument(
        "--legacy-cdn",
        metavar="SNAPSHOT",
        help="download a legacy Bunny CDN .db.gz snapshot instead of HF",
    )
    parser.add_argument("--keep-compressed", action="store_true")
    args = parser.parse_args()

    if args.legacy_cdn:
        out = Path(args.out or "data/openmarket.db")
        result = download_legacy_cdn(args.legacy_cdn, out, args.keep_compressed)
        print(result)
        return 0

    out_dir = Path(args.out or tempfile.mkdtemp(prefix="openmarket_hf_"))
    root = download_hf_split(args.split, out_dir)
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())