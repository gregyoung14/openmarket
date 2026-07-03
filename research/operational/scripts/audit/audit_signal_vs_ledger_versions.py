#!/usr/bin/env python3
"""
Correlate execution-engine runtime signal versions with ledger signal_version tags.

This script parses execution-engine entry logs (market slug + signal version) and
joins by ledger slug to show where tags diverged.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ENTRY_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T[^ ]+).*ENTRY signal received.*market.*Some\(\"(btc-updown-15m-\d+)\"\).*version.*Some\(\"([^\"]+)\"\)"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_iso_utc(ts: str) -> datetime:
    cleaned = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_runtime_slug_version_map(log_path: Path) -> dict[str, str]:
    slug_versions: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for raw in log_path.read_text(errors="ignore").splitlines():
        line = ANSI_RE.sub("", raw)
        m = ENTRY_RE.search(line)
        if not m:
            continue
        ts, slug, version = m.groups()
        slug_versions[slug].append((ts, version))

    # If a slug appears multiple times, use the latest timestamp.
    return {
        slug: sorted(rows, key=lambda item: item[0])[-1][1]
        for slug, rows in slug_versions.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        default="data/trade_ledger.json",
        help="Path to trade ledger JSON",
    )
    parser.add_argument(
        "--exec-log",
        default="logs/execution-engine.log",
        help="Path to execution engine log",
    )
    parser.add_argument(
        "--cutoff",
        default="2026-03-01T21:13:58+00:00",
        help="Only include ledger entries at/after this UTC ISO timestamp",
    )
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    log_path = Path(args.exec_log)
    cutoff = parse_iso_utc(args.cutoff)

    runtime_by_slug = build_runtime_slug_version_map(log_path)
    ledger = json.loads(ledger_path.read_text())

    pairs: Counter[tuple[str, str]] = Counter()
    post_cutoff_total = 0
    missing_runtime = 0

    for entry in ledger:
        redeemed_at = entry.get("redeemed_at")
        slug = entry.get("slug")
        ledger_version = entry.get("signal_version", "<missing>")
        if not isinstance(redeemed_at, str) or not isinstance(slug, str):
            continue

        dt = parse_iso_utc(redeemed_at)
        if dt < cutoff:
            continue

        post_cutoff_total += 1
        runtime_version = runtime_by_slug.get(slug)
        if runtime_version is None:
            missing_runtime += 1
            continue
        pairs[(runtime_version, ledger_version)] += 1

    matched = sum(c for (runtime, ledger_v), c in pairs.items() if runtime == ledger_v)
    compared = sum(pairs.values())

    print(f"post_cutoff_entries={post_cutoff_total}")
    print(f"compared_entries={compared}")
    print(f"exact_matches={matched}")
    print(f"mismatches={compared - matched}")
    print(f"missing_runtime_for_slug={missing_runtime}")
    print()
    print("runtime_version|ledger_version|count")
    for (runtime, ledger_v), count in pairs.most_common():
        print(f"{runtime}|{ledger_v}|{count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
