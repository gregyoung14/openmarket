#!/usr/bin/env python3
"""
Audit missing ledger redeems using Polymarket activity API as source of truth.

This avoids explorer txlist truncation issues by using paginated activity rows.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import requests
from dotenv import dotenv_values
from eth_account import Account

LEDGER_DEFAULT = Path("data/trade_ledger.json")
REPORT_DEFAULT = Path("data/redeem_activity_ledger_audit.json")
CSV_DEFAULT = Path("data/missing_redeems_from_activity.csv")


def norm_hash(h: str) -> str:
    v = (h or "").strip().lower()
    if not v:
        return ""
    return v if v.startswith("0x") else f"0x{v}"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--wallet", default=None)
    p.add_argument("--ledger", default=str(LEDGER_DEFAULT))
    p.add_argument("--report", default=str(REPORT_DEFAULT))
    p.add_argument("--csv", default=str(CSV_DEFAULT))
    p.add_argument("--chunk", type=int, default=500)
    return p.parse_args()


def wallet_from_env() -> str:
    cfg = dotenv_values(".env.local")
    pk = (cfg.get("POLYGON_PRIVATE_KEY") or "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    return Account.from_key(pk).address if pk else ""


def fetch_all_activity(wallet: str, chunk: int) -> List[Dict]:
    rows: List[Dict] = []
    offset = 0
    while True:
        resp = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": wallet, "limit": chunk, "offset": offset},
            timeout=120,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < chunk:
            break
        offset += chunk
    return rows


def main() -> int:
    args = parse_args()
    wallet = args.wallet or wallet_from_env()
    if not wallet:
        print("ERROR: wallet not provided and not found in .env.local")
        return 1

    ledger_path = Path(args.ledger)
    report_path = Path(args.report)
    csv_path = Path(args.csv)

    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    ledger_success = [r for r in ledger if str(r.get("tx_status", "")).upper() == "SUCCESS"]
    ledger_hashes = {norm_hash(str(r.get("tx_hash", ""))) for r in ledger_success if norm_hash(str(r.get("tx_hash", "")))}

    activity = fetch_all_activity(wallet, args.chunk)
    activity_types = Counter(r.get("type") for r in activity)

    redeems = [r for r in activity if r.get("type") == "REDEEM"]
    redeem_hashes = {norm_hash(str(r.get("transactionHash", ""))) for r in redeems if norm_hash(str(r.get("transactionHash", "")))}

    missing_hashes = sorted(redeem_hashes - ledger_hashes)
    extra_hashes = sorted(ledger_hashes - redeem_hashes)

    missing_rows = []
    by_hash = {norm_hash(str(r.get("transactionHash", ""))): r for r in redeems}
    for h in missing_hashes:
        r = by_hash.get(h, {})
        ts = r.get("timestamp")
        ts_iso = ""
        try:
            ts_iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        except Exception:
            pass
        missing_rows.append(
            {
                "transactionHash": h,
                "timestamp": ts,
                "timestamp_iso": ts_iso,
                "conditionId": r.get("conditionId", ""),
                "slug": r.get("slug", ""),
                "outcome": r.get("outcome", ""),
                "size": r.get("size", ""),
                "price": r.get("price", ""),
                "usdcSize": r.get("usdcSize", ""),
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wallet": wallet,
        "summary": {
            "activity_total_rows": len(activity),
            "activity_types": dict(activity_types),
            "activity_redeem_unique_hashes": len(redeem_hashes),
            "ledger_success_unique_hashes": len(ledger_hashes),
            "missing_redeems_in_ledger": len(missing_hashes),
            "ledger_success_not_in_activity_redeem": len(extra_hashes),
        },
        "missing_redeem_hashes": missing_hashes,
        "ledger_extra_hashes": extra_hashes,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "transactionHash",
                "timestamp",
                "timestamp_iso",
                "conditionId",
                "slug",
                "outcome",
                "size",
                "price",
                "usdcSize",
            ],
        )
        writer.writeheader()
        writer.writerows(missing_rows)

    print("=== Redeem Activity vs Ledger Audit ===")
    for k, v in report["summary"].items():
        print(f"{k}: {v}")
    print(f"Report: {report_path}")
    print(f"CSV: {csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
