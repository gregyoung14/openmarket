#!/usr/bin/env python3
"""Bump the OpenMarket HF dataset version.

Reads `datasets/hf/README.md`, increments the `Dataset version` field in the
"Release artifacts" section, and writes the updated card. Designed to be
called after a new export/upload to the dataset repo.

Usage:
    .venv/bin/python scripts/hf/bump_dataset_version.py --set v0.2-full
    .venv/bin/python scripts/hf/bump_dataset_version.py --bump minor
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


CARD_PATH = Path("datasets/hf/README.md")
VERSION_RE = re.compile(r"(Dataset version:\s*)([^\s`]+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", help="explicit version string (e.g. v0.2-full)")
    group.add_argument("--bump", choices=("major", "minor", "patch"),
                       help="increment an existing semver-like version")
    parser.add_argument("--card", default=str(CARD_PATH), type=Path)
    return parser.parse_args()


def parse_version(text: str) -> tuple[int, int, int, str | None]:
    m = re.search(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:-(.+))?", text)
    if not m:
        raise SystemExit(f"could not parse version from: {text!r}")
    major, minor, patch, suffix = m.groups()
    return int(major), int(minor), int(patch or 0), suffix


def format_version(major: int, minor: int, patch: int, suffix: str | None) -> str:
    base = f"v{major}.{minor}.{patch}"
    return f"{base}-{suffix}" if suffix else base


def bump(text: str, kind: str) -> str:
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"no 'Dataset version:' line in {CARD_PATH}")
    major, minor, patch, suffix = parse_version(m.group(2))
    if kind == "major":
        major += 1
        minor = patch = 0
    elif kind == "minor":
        minor += 1
        patch = 0
    elif kind == "patch":
        patch += 1
    new = format_version(major, minor, patch, suffix)
    return VERSION_RE.sub(rf"\g<1>{new}", text, count=1)


def set_version(text: str, value: str) -> str:
    if not VERSION_RE.search(text):
        raise SystemExit(f"no 'Dataset version:' line in {CARD_PATH}")
    return VERSION_RE.sub(rf"\g<1>{value}", text, count=1)


def main() -> int:
    args = parse_args()
    card = args.card
    original = card.read_text(encoding="utf-8")
    updated = set_version(original, args.set) if args.set else bump(original, args.bump)
    if updated == original:
        print("no change")
        return 0
    card.write_text(updated, encoding="utf-8")
    new_version = VERSION_RE.search(updated).group(2)
    print(f"{card}: Dataset version -> {new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())