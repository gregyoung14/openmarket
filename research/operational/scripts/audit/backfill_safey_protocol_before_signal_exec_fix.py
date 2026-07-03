#!/usr/bin/env python3
"""
Backfill ledger entries before the signal/execution pipeline bug-fix timestamp.

Default cutoff comes from commit 53acef4:
  2026-03-01 16:13:58 -0500  == 2026-03-01T21:13:58+00:00

Usage:
  python3 scripts/backfill_safey_protocol_before_signal_exec_fix.py
  python3 scripts/backfill_safey_protocol_before_signal_exec_fix.py --apply
  python3 scripts/backfill_safey_protocol_before_signal_exec_fix.py --cutoff 2026-03-01T21:13:58+00:00
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "trade_ledger.json"
DEFAULT_CUTOFF = "2026-03-01T21:13:58+00:00"
NEW_TAG = "safey-protocol"
BACKUP_SUFFIX_PREFIX = ".bak-safey-protocol-"


def parse_iso(value: str) -> datetime:
    # Accept trailing Z and timezone-aware ISO strings.
    cleaned = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes to ledger")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="UTC ISO timestamp cutoff")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    cutoff_dt = parse_iso(args.cutoff)

    print(f"=== Backfill Safey Protocol ({mode}) ===")
    print(f"Ledger:  {LEDGER_PATH}")
    print(f"Cutoff:  {cutoff_dt.isoformat()} (entries before this are retagged)")
    print(f"New tag: {NEW_TAG}")
    print()

    if not LEDGER_PATH.exists():
        print(f"ERROR: Ledger not found at {LEDGER_PATH}")
        return 1

    with open(LEDGER_PATH) as f:
        ledger: List[Dict[str, Any]] = json.load(f)

    total = len(ledger)
    eligible = 0
    changed = 0
    already = 0
    skipped_bad_time = 0

    for entry in ledger:
        redeemed_at = entry.get("redeemed_at")
        if not isinstance(redeemed_at, str):
            skipped_bad_time += 1
            continue

        try:
            redeemed_dt = parse_iso(redeemed_at)
        except Exception:
            skipped_bad_time += 1
            continue

        if redeemed_dt >= cutoff_dt:
            continue

        eligible += 1
        current = entry.get("signal_version")
        if current == NEW_TAG:
            already += 1
            continue

        changed += 1
        if args.apply:
            entry["signal_version"] = NEW_TAG

    print(f"Total entries:          {total}")
    print(f"Eligible before cutoff: {eligible}")
    print(f"Changed:                {changed}")
    print(f"Already {NEW_TAG}:      {already}")
    print(f"Skipped bad timestamps: {skipped_bad_time}")

    if args.apply:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = LEDGER_PATH.with_name(LEDGER_PATH.name + BACKUP_SUFFIX_PREFIX + timestamp)
        shutil.copy2(LEDGER_PATH, backup_path)
        with open(LEDGER_PATH, "w") as f:
            json.dump(ledger, f, indent=2)
        print()
        print(f"Backup written: {backup_path}")
        print(f"Updated ledger: {LEDGER_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
