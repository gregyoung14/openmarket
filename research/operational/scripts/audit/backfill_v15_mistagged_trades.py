#!/usr/bin/env python3
"""
Targeted ledger backfill: retag known v15 trades that were mislabeled as v8-fix.

Usage:
  python3 scripts/backfill_v15_mistagged_trades.py           # dry-run
  python3 scripts/backfill_v15_mistagged_trades.py --apply   # apply changes
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "trade_ledger.json"
OLD_TAG = "v8-fix"
NEW_TAG = "v15"
BACKUP_SUFFIX_PREFIX = ".bak-v15-retag-"

# Trade IDs provided in incident report (v15 runtime but tagged v8-fix).
TARGET_CONDITION_IDS = {
    "REDACTED_CONDITION_ID_983",  # 983
    "REDACTED_CONDITION_ID_984",  # 984
    "REDACTED_CONDITION_ID_985",  # 985
    "REDACTED_CONDITION_ID_986",  # 986
    "REDACTED_CONDITION_ID_987",  # 987
    "REDACTED_CONDITION_ID_988",  # 988
    "REDACTED_CONDITION_ID_989",  # 989
    "REDACTED_CONDITION_ID_990",  # 990
    "REDACTED_CONDITION_ID_991",  # 991
    "REDACTED_CONDITION_ID_992",  # 992
    "REDACTED_CONDITION_ID_993",  # 993
    "REDACTED_CONDITION_ID_994",  # 994
    "REDACTED_CONDITION_ID_995",  # 995
}


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"

    print(f"=== Targeted v15 Ledger Retag ({mode}) ===")
    print(f"Ledger: {LEDGER_PATH}")
    print(f"Retag: {OLD_TAG} -> {NEW_TAG}")
    print(f"Target condition IDs: {len(TARGET_CONDITION_IDS)}")
    print()

    if not LEDGER_PATH.exists():
        print(f"ERROR: Ledger not found at {LEDGER_PATH}")
        return 1

    with open(LEDGER_PATH) as f:
        ledger = json.load(f)

    total_entries = len(ledger)
    touched = 0
    already_new = 0
    matched_target_ids = set()

    for entry in ledger:
        condition_id = entry.get("condition_id")
        if condition_id not in TARGET_CONDITION_IDS:
            continue

        matched_target_ids.add(condition_id)
        old = entry.get("signal_version")

        if old == NEW_TAG:
            already_new += 1
            continue

        if old == OLD_TAG:
            touched += 1
            if apply:
                entry["signal_version"] = NEW_TAG
            print(
                f"RETAG {entry.get('redeemed_at')} | {entry.get('slug')} | "
                f"condition={condition_id[:14]}... | {old} -> {NEW_TAG}"
            )

    missing_target_ids = TARGET_CONDITION_IDS - matched_target_ids

    print()
    print(f"Total ledger entries:      {total_entries}")
    print(f"Retagged entries:          {touched}")
    print(f"Already {NEW_TAG}:         {already_new}")
    print(f"Missing target IDs:        {len(missing_target_ids)}")

    if missing_target_ids:
        print("Missing condition IDs:")
        for cid in sorted(missing_target_ids):
            print(f"  - {cid}")

    if apply:
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
