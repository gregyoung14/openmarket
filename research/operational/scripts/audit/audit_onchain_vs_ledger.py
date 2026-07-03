#!/usr/bin/env python3
"""
Audit wallet on-chain Polygon transactions vs local trade ledger.

What it does:
1) Pulls full wallet tx history from Blockscout API (no API key required)
2) Fetches on-chain tx + receipt details via Polygon RPC
3) Decodes ConditionalTokens.redeemPositions inputs
4) Compares on-chain redeem tx hashes against ledger SUCCESS tx hashes
5) Writes JSON and CSV reports for reconciliation/backfill

Usage:
  python3 scripts/audit_onchain_vs_ledger.py
  python3 scripts/audit_onchain_vs_ledger.py --wallet 0x... --apply-backfill
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import dotenv_values
from eth_account import Account
from web3 import Web3

BLOCKSCOUT_API = "https://polygon.blockscout.com/api"
LEDGER_PATH = Path("data/trade_ledger.json")
REPORT_JSON_PATH = Path("data/onchain_ledger_audit.json")
MISSING_CSV_PATH = Path("data/missing_onchain_redeems.csv")

CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

RPC_ENDPOINTS = [
    os.environ.get("POLYGON_RPC", "").strip() or "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.drpc.org",
]

CONDITIONAL_TOKENS_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex().lower()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", default=None, help="Wallet address (defaults from .env.local private key)")
    parser.add_argument("--ledger", default=str(LEDGER_PATH), help="Ledger path")
    parser.add_argument("--report-json", default=str(REPORT_JSON_PATH), help="Output JSON report")
    parser.add_argument("--missing-csv", default=str(MISSING_CSV_PATH), help="Output CSV for missing redeems")
    parser.add_argument("--offset", type=int, default=1000, help="Blockscout page size")
    return parser.parse_args()


def load_wallet_from_env(env_path: Path) -> Optional[str]:
    cfg = dotenv_values(str(env_path))
    pk = (cfg.get("POLYGON_PRIVATE_KEY") or "").strip()
    if not pk:
        return None
    if pk.startswith("0x"):
        pk = pk[2:]
    return Account.from_key(pk).address


class RpcClient:
    def __init__(self, endpoints: List[str]):
        unique = [e for e in dict.fromkeys([x for x in endpoints if x])]
        self.endpoints = unique
        self.idx = 0
        self.w3 = Web3(Web3.HTTPProvider(self.endpoints[self.idx], request_kwargs={"timeout": 20}))

    def _rotate(self):
        self.idx = (self.idx + 1) % len(self.endpoints)
        self.w3 = Web3(Web3.HTTPProvider(self.endpoints[self.idx], request_kwargs={"timeout": 20}))

    def call_with_retry(self, fn, attempts: int = 8, sleep_s: float = 0.25):
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                return fn()
            except Exception as exc:
                last = exc
                self._rotate()
                time.sleep(sleep_s)
        raise RuntimeError(f"RPC failed after retries: {last}")

    def get_tx(self, tx_hash: str) -> Optional[dict]:
        try:
            return self.call_with_retry(lambda: self.w3.eth.get_transaction(tx_hash))
        except Exception:
            return None

    def get_receipt(self, tx_hash: str) -> Optional[dict]:
        try:
            return self.call_with_retry(lambda: self.w3.eth.get_transaction_receipt(tx_hash))
        except Exception:
            return None


def fetch_wallet_txs(wallet: str, offset: int = 1000) -> List[dict]:
    txs: List[dict] = []
    page = 1
    while True:
        params = {
            "module": "account",
            "action": "txlist",
            "address": wallet,
            "sort": "asc",
            "page": page,
            "offset": offset,
        }
        resp = requests.get(BLOCKSCOUT_API, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("result", [])
        if not isinstance(batch, list) or not batch:
            break
        txs.extend(batch)
        if len(batch) < offset:
            break
        page += 1
    return txs


def norm_hash(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("0x"):
        return h
    if h:
        return "0x" + h
    return ""


def parse_ts(ts: str) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def decode_redeem_input(contract, tx_input: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        fn, args = contract.decode_function_input(tx_input)
        if fn.fn_name != "redeemPositions":
            return out
        out = {
            "method": "redeemPositions",
            "collateralToken": str(args.get("collateralToken", "")),
            "parentCollectionId": args.get("parentCollectionId", b"").hex() if args.get("parentCollectionId") else "",
            "conditionId": args.get("conditionId", b"").hex() if args.get("conditionId") else "",
            "indexSets": [int(x) for x in args.get("indexSets", [])],
        }
    except Exception:
        pass
    return out


def decode_usdc_to_wallet(receipt: Optional[dict], wallet: str) -> Optional[float]:
    if not receipt:
        return None
    wallet = wallet.lower()
    usdc = USDC_ADDRESS.lower()
    total_raw = 0
    for log in receipt.get("logs", []):
        if str(log.get("address", "")).lower() != usdc:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        t0 = Web3.to_hex(topics[0]).lower()
        if t0 != TRANSFER_TOPIC:
            continue
        to_addr = "0x" + Web3.to_hex(topics[2])[-40:].lower()
        if to_addr != wallet:
            continue
        total_raw += int(Web3.to_hex(log.get("data", b"0x0")), 16)
    return float(total_raw) / 1_000_000.0


def load_ledger(path: Path) -> List[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else []


def main() -> int:
    args = parse_args()

    wallet = args.wallet
    if not wallet:
        wallet = load_wallet_from_env(Path(".env.local"))
    if not wallet:
        print("ERROR: Wallet not provided and could not derive from .env.local")
        return 1

    wallet = Web3.to_checksum_address(wallet)
    print(f"Wallet: {wallet}")

    ledger_path = Path(args.ledger)
    report_json_path = Path(args.report_json)
    missing_csv_path = Path(args.missing_csv)

    ledger = load_ledger(ledger_path)
    ledger_success = [r for r in ledger if str(r.get("tx_status", "")).upper() == "SUCCESS"]
    ledger_by_tx: Dict[str, List[dict]] = defaultdict(list)
    for row in ledger_success:
        txh = norm_hash(str(row.get("tx_hash", "")))
        if txh:
            ledger_by_tx[txh].append(row)

    txs = fetch_wallet_txs(wallet, offset=args.offset)
    print(f"Fetched wallet tx count: {len(txs)}")

    rpc = RpcClient(RPC_ENDPOINTS)
    ctf = rpc.w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS), abi=CONDITIONAL_TOKENS_ABI)

    method_counts: Counter[str] = Counter()
    redeem_txs: List[dict] = []
    detailed_rows: List[dict] = []

    for tx in txs:
        tx_hash = norm_hash(str(tx.get("hash", "")))
        to_addr = str(tx.get("to", "") or "").lower()
        from_addr = str(tx.get("from", "") or "").lower()
        tx_input = str(tx.get("input", "") or "0x")
        selector = tx_input[:10].lower() if len(tx_input) >= 10 else "0x"

        method = "native_transfer" if tx_input in ("0x", "") else selector
        method_counts[method] += 1

        is_redeem = (to_addr == CONDITIONAL_TOKENS_ADDRESS.lower() and selector == "0x01b7037c")

        tx_full = rpc.get_tx(tx_hash)
        receipt = rpc.get_receipt(tx_hash)
        status = int(receipt.get("status", 0)) if receipt is not None else int(tx.get("txreceipt_status", 0) or 0)
        gas_used = int(receipt.get("gasUsed", 0)) if receipt is not None else int(tx.get("gasUsed", 0) or 0)
        gas_price = int(tx_full.get("gasPrice", 0)) if tx_full is not None else int(tx.get("gasPrice", 0) or 0)
        gas_native = (gas_used * gas_price) / 1e18
        value_native = int(tx_full.get("value", 0)) / 1e18 if tx_full is not None else int(tx.get("value", 0) or 0) / 1e18

        row = {
            "hash": tx_hash,
            "block_number": int(tx.get("blockNumber", 0) or 0),
            "timestamp": int(tx.get("timeStamp", 0) or 0),
            "timestamp_iso": parse_ts(str(tx.get("timeStamp", ""))),
            "from": from_addr,
            "to": to_addr,
            "status": status,
            "selector": selector,
            "is_redeem_positions": is_redeem,
            "value_pol": value_native,
            "gas_used": gas_used,
            "gas_price_wei": gas_price,
            "gas_cost_pol": gas_native,
        }

        if is_redeem:
            decoded = decode_redeem_input(ctf, tx_input)
            payout_to_wallet = decode_usdc_to_wallet(receipt, wallet)
            row.update(
                {
                    "decoded_method": decoded.get("method", "redeemPositions"),
                    "condition_id": ("0x" + decoded.get("conditionId", "")) if decoded.get("conditionId") else "",
                    "index_sets": decoded.get("indexSets", []),
                    "collateral_token": decoded.get("collateralToken", ""),
                    "onchain_usdc_to_wallet": payout_to_wallet,
                    "ledger_rows": len(ledger_by_tx.get(tx_hash, [])),
                    "in_ledger": tx_hash in ledger_by_tx,
                    "receipt_available": receipt is not None,
                }
            )
            redeem_txs.append(row)

        detailed_rows.append(row)

    onchain_redeem_hashes = {r["hash"] for r in redeem_txs if r.get("status") == 1}
    ledger_success_hashes = set(ledger_by_tx.keys())

    missing_in_ledger = sorted(onchain_redeem_hashes - ledger_success_hashes)
    ledger_not_onchain = sorted(ledger_success_hashes - onchain_redeem_hashes)

    missing_rows = [r for r in redeem_txs if r["hash"] in set(missing_in_ledger)]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wallet": wallet,
        "summary": {
            "wallet_tx_count": len(txs),
            "wallet_redeem_txs": len(redeem_txs),
            "wallet_redeem_success_txs": len(onchain_redeem_hashes),
            "ledger_total_rows": len(ledger),
            "ledger_success_rows": len(ledger_success),
            "ledger_success_unique_txs": len(ledger_success_hashes),
            "missing_redeem_txs_in_ledger": len(missing_in_ledger),
            "ledger_success_txs_not_found_onchain": len(ledger_not_onchain),
        },
        "method_counts": dict(method_counts),
        "missing_redeem_tx_hashes": missing_in_ledger,
        "ledger_orphan_tx_hashes": ledger_not_onchain,
        "missing_redeem_details": missing_rows,
    }

    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.write_text(json.dumps(report, indent=2))

    missing_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(missing_csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "hash",
                "timestamp_iso",
                "block_number",
                "status",
                "condition_id",
                "index_sets",
                "onchain_usdc_to_wallet",
                "gas_cost_pol",
                "from",
                "to",
            ],
        )
        writer.writeheader()
        for row in missing_rows:
            writer.writerow(
                {
                    "hash": row.get("hash"),
                    "timestamp_iso": row.get("timestamp_iso"),
                    "block_number": row.get("block_number"),
                    "status": row.get("status"),
                    "condition_id": row.get("condition_id"),
                    "index_sets": json.dumps(row.get("index_sets", [])),
                    "onchain_usdc_to_wallet": row.get("onchain_usdc_to_wallet"),
                    "gas_cost_pol": row.get("gas_cost_pol"),
                    "from": row.get("from"),
                    "to": row.get("to"),
                }
            )

    print("\n=== On-Chain vs Ledger Audit Summary ===")
    for k, v in report["summary"].items():
        print(f"{k}: {v}")
    print(f"Report JSON: {report_json_path}")
    print(f"Missing CSV: {missing_csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
