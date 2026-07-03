#!/usr/bin/env python3
"""
Normalize post-cutoff ledger signal_version values from execution runtime logs.

This preserves runtime truth by setting each post-cutoff ledger row's
`signal_version` to the version observed in execution ENTRY logs for the row's slug.

Usage:
  python3 scripts/backfill_signal_versions_from_runtime_post_cutoff.py
  python3 scripts/backfill_signal_versions_from_runtime_post_cutoff.py --apply
  python3 scripts/backfill_signal_versions_from_runtime_post_cutoff.py --cutoff 2026-03-01T21:13:58+00:00
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path("data/trade_ledger.json")
EXEC_LOG_PATH = Path("logs/execution-engine.log")
DEFAULT_CUTOFF = "2026-03-01T21:13:58+00:00"
BACKUP_SUFFIX_PREFIX = ".bak-signal-runtime-normalize-"

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

    return {
        slug: sorted(rows, key=lambda item: item[0])[-1][1]
        for slug, rows in slug_versions.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=str(LEDGER_PATH), help="Path to trade ledger JSON")
    parser.add_argument("--exec-log", default=str(EXEC_LOG_PATH), help="Path to execution engine log")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="UTC ISO timestamp cutoff")
    parser.add_argument("--apply", action="store_true", help="Write changes to ledger")
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    exec_log_path = Path(args.exec_log)
    cutoff = parse_iso_utc(args.cutoff)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Signal Version Runtime Normalization ({mode}) ===")
    print(f"Ledger: {ledger_path}")
    print(f"Exec log: {exec_log_path}")
    print(f"Cutoff: {cutoff.isoformat()} (inclusive)")
    print()

    if not ledger_path.exists():
        print(f"ERROR: Ledger not found at {ledger_path}")
        return 1
    if not exec_log_path.exists():
        print(f"ERROR: Execution log not found at {exec_log_path}")
        return 1

    runtime_by_slug = build_runtime_slug_version_map(exec_log_path)
    ledger = json.loads(ledger_path.read_text())

    total = len(ledger)
    post_cutoff = 0
    changed = 0
    already_matching = 0
    missing_runtime = 0
    bad_timestamps = 0
    transitions: Counter[tuple[str, str]] = Counter()

    for entry in ledger:
        redeemed_at = entry.get("redeemed_at")
        slug = entry.get("slug")
        if not isinstance(redeemed_at, str) or not isinstance(slug, str):
            continue

        try:
            redeemed_dt = parse_iso_utc(redeemed_at)
        except Exception:
            bad_timestamps += 1
            continue

        if redeemed_dt < cutoff:
            continue

        post_cutoff += 1
        runtime_version = runtime_by_slug.get(slug)
        if runtime_version is None:
            missing_runtime += 1
            continue

        current = entry.get("signal_version", "<missing>")
        if current == runtime_version:
            already_matching += 1
            continue

        changed += 1
        transitions[(str(current), runtime_version)] += 1
        if args.apply:
            entry["signal_version"] = runtime_version

    print(f"Total ledger rows:             {total}")
    print(f"Post-cutoff rows:              {post_cutoff}")
    print(f"Changed rows:                  {changed}")
    print(f"Already matching runtime:      {already_matching}")
    print(f"Missing runtime mapping (slug): {missing_runtime}")
    print(f"Bad timestamps skipped:        {bad_timestamps}")
    print()
    print("from_signal_version|to_runtime_version|count")
    for (old, new), count in transitions.most_common():
        print(f"{old}|{new}|{count}")

    if args.apply:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = ledger_path.with_name(ledger_path.name + BACKUP_SUFFIX_PREFIX + timestamp)
        shutil.copy2(ledger_path, backup_path)
        ledger_path.write_text(json.dumps(ledger, indent=2))
        print()
        print(f"Backup written: {backup_path}")
        print(f"Updated ledger: {ledger_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
