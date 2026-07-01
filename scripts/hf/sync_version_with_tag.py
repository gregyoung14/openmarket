#!/usr/bin/env python3
"""Sync the published HF dataset card version with the current Git tag.

Reads the latest `v*` tag from git, parses the dataset card at
`datasets/hf/README.md`, and either:

  - updates the `Dataset version:` field to match the git tag, OR
  - verifies that the dataset card already matches (default, used in CI)

Designed to be the last step before pushing a release: it ties the
GitHub source tag to the Hugging Face dataset card so consumers can
reproduce a paper or benchmark by pinning either side.

Usage:
    .venv/bin/python scripts/hf/sync_version_with_tag.py --tag v0.2.0
    .venv/bin/python scripts/hf/sync_version_with_tag.py           # auto-detect tag
    .venv/bin/python scripts/hf/sync_version_with_tag.py --check   # CI mode: exit 1 if mismatch
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


CARD_PATH = Path("datasets/hf/README.md")
VERSION_LINE_RE = re.compile(r"(Dataset version:\s*)([^\s`]+)")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][\w.]+)?$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="explicit tag; if omitted, uses the latest `v*` tag from git")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the dataset card does not already match the tag")
    parser.add_argument("--dataset-version",
                        help="explicit dataset version string to write (default: same as --tag)")
    parser.add_argument("--allow-mismatch", action="store_true",
                        help="in --check mode, exit 0 even if card and tag differ "
                             "(useful when the dataset is intentionally versioned independently, "
                             "e.g. v0.1-sample vs v0.1.0)")
    parser.add_argument("--card", default=str(CARD_PATH), type=Path)
    return parser.parse_args()


def latest_tag() -> str | None:
    out = subprocess.check_output(
        ["git", "tag", "--sort=-v:refname", "--list", "v*.*.*"],
        text=True,
    ).strip()
    return out.splitlines()[0] if out else None


def current_card_version(card_text: str) -> str | None:
    m = VERSION_LINE_RE.search(card_text)
    return m.group(2) if m else None


def main() -> int:
    args = parse_args()
    tag = args.tag or latest_tag()
    if not tag:
        print("no tag specified and no `v*.*.*` git tag found", file=sys.stderr)
        return 2
    if not TAG_RE.match(tag):
        print(f"tag does not look like semver: {tag!r}", file=sys.stderr)
        return 2

    if not args.card.exists():
        print(f"dataset card not found: {args.card}", file=sys.stderr)
        return 2

    text = args.card.read_text(encoding="utf-8")
    current = current_card_version(text)
    target = args.dataset_version or tag
    print(f"git tag:           {tag}")
    print(f"dataset card:      {args.card}")
    print(f"current version:   {current}")
    print(f"target version:    {target}")

    if current == target:
        print("OK (already in sync)")
        return 0

    if args.check:
        if args.allow_mismatch:
            print(f"NOTE: card and tag differ; --allow-mismatch set, exiting 0")
            return 0
        print("MISMATCH (CI check failed)", file=sys.stderr)
        return 1

    new_text = VERSION_LINE_RE.sub(rf"\g<1>{target}", text, count=1)
    args.card.write_text(new_text, encoding="utf-8")
    print(f"updated -> {target}")
    print(f"review the diff and commit before pushing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())