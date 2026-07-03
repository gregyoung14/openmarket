#!/usr/bin/env python3
"""
Backfill missing SUCCESS ledger rows from Polymarket REDEEM activity.

Source of truth:
- Polymarket activity endpoint (paginated): type == REDEEM
- Local ledger SUCCESS tx hashes

The script appends one ledger row per missing redeem tx hash.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from dotenv import dotenv_values
from eth_account import Account

LEDGER_DEFAULT = Path("data/trade_ledger.json")
BACKUP_SUFFIX_PREFIX = ".bak-activity-redeem-backfill-"


def norm_hash(h: str) -> str:
    v = (h or "").strip().lower()
    if not v:
        return ""
    return v if v.startswith("0x") else f"0x{v}"


def hash_without_prefix(h: str) -> str:
    n = norm_hash(h)
    return n[2:] if n.startswith("0x") else n


def wallet_from_env() -> str:
    cfg = dotenv_values(".env.local")
    pk = (cfg.get("POLYGON_PRIVATE_KEY") or "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    return Account.from_key(pk).address if pk else ""


def parse_iso(ts: str) -> datetime:
    cleaned = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def fetch_all_activity(wallet: str, chunk: int = 500) -> List[Dict]:
    out: List[Dict] = []
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
        out.extend(batch)
        if len(batch) < chunk:
            break
        offset += chunk
    return out


def infer_versions(redeemed_at: datetime, ledger_rows: List[Dict]) -> Tuple[str, str]:
    best_idx = None
    best_dist = None
    for i, row in enumerate(ledger_rows):
        ts = row.get("redeemed_at")
        if not isinstance(ts, str):
            continue
        try:
            dt = parse_iso(ts)
        except Exception:
            continue
        dist = abs((dt - redeemed_at).total_seconds())
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_idx = i

    if best_idx is None:
        return ("unknown", "unknown")

    ref = ledger_rows[best_idx]
    signal_version = str(ref.get("signal_version", "unknown") or "unknown")
    execution_version = str(ref.get("execution_version", "unknown") or "unknown")
    return (signal_version, execution_version)


def build_row_from_activity(activity_row: Dict, signal_version: str, execution_version: str) -> Dict:
    size = to_float(activity_row.get("size"), 0.0)
    avg_price = to_float(activity_row.get("price"), 0.0)
    usdc_size = to_float(activity_row.get("usdcSize"), 0.0)

    initial_value = size * avg_price if size > 0 and avg_price > 0 else 0.0
    current_value = usdc_size
    cash_pnl = current_value - initial_value
    percent_pnl = (cash_pnl / initial_value * 100.0) if initial_value > 0 else 0.0

    if size > 0:
        settlement_price = current_value / size
        cur_price = settlement_price
    else:
        settlement_price = 1.0 if current_value > 0 else 0.0
        cur_price = settlement_price

    settlement_won = settlement_price >= 0.99

    ts = int(activity_row.get("timestamp", 0) or 0)
    redeemed_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts > 0 else datetime.now(timezone.utc).isoformat()

    return {
        "redeemed_at": redeemed_at,
        "slug": activity_row.get("slug", "unknown"),
        "title": activity_row.get("title", ""),
        "outcome": activity_row.get("outcome") or "Unknown",
        "won": cash_pnl > 0,
        "settlement_won": settlement_won,
        "size": round(size, 6),
        "avg_price": round(avg_price, 6),
        "initial_value": round(initial_value, 6),
        "current_value": round(current_value, 6),
        "cash_pnl": round(cash_pnl, 6),
        "percent_pnl": round(percent_pnl, 6),
        "cur_price": round(cur_price, 6),
        "settlement_price": round(settlement_price, 6),
        "onchain_payout_usdc": round(usdc_size, 6),
        "payout_source": "polymarket_activity_backfill",
        "condition_id": activity_row.get("conditionId", ""),
        "tx_hash": hash_without_prefix(activity_row.get("transactionHash", "")),
        "tx_status": "SUCCESS",
        "usdc_before": None,
        "usdc_after": None,
        "signal_version": signal_version,
        "execution_version": execution_version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", default=None)
    parser.add_argument("--ledger", default=str(LEDGER_DEFAULT))
    parser.add_argument("--chunk", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    wallet = args.wallet or wallet_from_env()
    if not wallet:
        print("ERROR: wallet not provided and not found in .env.local")
        return 1

    ledger_path = Path(args.ledger)
    if not ledger_path.exists():
        print(f"ERROR: ledger not found: {ledger_path}")
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Backfill Missing Redeems From Activity ({mode}) ===")
    print(f"Wallet: {wallet}")
    print(f"Ledger: {ledger_path}")

    ledger_rows = json.loads(ledger_path.read_text())
    success_hashes = {
        norm_hash(str(r.get("tx_hash", "")))
        for r in ledger_rows
        if str(r.get("tx_status", "")).upper() == "SUCCESS" and norm_hash(str(r.get("tx_hash", "")))
    }

    activity = fetch_all_activity(wallet, chunk=args.chunk)
    redeem_rows = [r for r in activity if r.get("type") == "REDEEM" and norm_hash(str(r.get("transactionHash", "")))]
    redeem_by_hash = {norm_hash(str(r.get("transactionHash", ""))): r for r in redeem_rows}

    missing_hashes = sorted(set(redeem_by_hash.keys()) - success_hashes)

    print(f"Activity rows:                 {len(activity)}")
    print(f"Activity REDEEM rows:          {len(redeem_rows)}")
    print(f"Ledger SUCCESS unique hashes:  {len(success_hashes)}")
    print(f"Missing hashes to backfill:    {len(missing_hashes)}")

    append_rows: List[Dict] = []
    for h in missing_hashes:
        a = redeem_by_hash[h]
        ts = int(a.get("timestamp", 0) or 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else datetime.now(timezone.utc)
        sig_ver, exe_ver = infer_versions(dt, ledger_rows)
        row = build_row_from_activity(a, sig_ver, exe_ver)
        append_rows.append(row)

    if append_rows:
        print("Sample appended rows:")
        for r in append_rows[:5]:
            print(f"  {r['redeemed_at']} | {r['slug']} | tx={r['tx_hash'][:12]}... | payout={r['onchain_payout_usdc']}")

    if args.apply and append_rows:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = ledger_path.with_name(ledger_path.name + BACKUP_SUFFIX_PREFIX + stamp)
        shutil.copy2(ledger_path, backup_path)

        updated = list(ledger_rows)
        updated.extend(append_rows)
        ledger_path.write_text(json.dumps(updated, indent=2))

        print(f"Backup written: {backup_path}")
        print(f"Ledger updated: {ledger_path}")
        print(f"Rows appended:  {len(append_rows)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
