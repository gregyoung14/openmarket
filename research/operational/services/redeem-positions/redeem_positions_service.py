#!/usr/bin/env python3
"""
Polymarket Position Redemption Service (recurring)

Runs on a loop every POLL_INTERVAL_SECS (default 120s):
  1) Fetch all redeemable positions from the Polymarket Data API
  2) Log each position as source-of-truth (slug, outcome, size, avgPrice, pnl)
  3) Redeem each unique condition on-chain via ConditionalTokens.redeemPositions()
  4) Append redeemed trades to a JSON trade ledger
  5) Expose /health endpoint for Uptime Kuma

This is the DEFINITIVE source of truth for win/loss tracking — it uses
Polymarket's own resolution data, not our internal signal logs.

Usage:
  python3 services/redeem-positions/redeem_positions_service.py             # live service
  python3 services/redeem-positions/redeem_positions_service.py --dry-run   # no on-chain tx
  python3 services/redeem-positions/redeem_positions_service.py --once      # run once and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware


# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════
ENV_PATH = Path(__file__).resolve().parents[2] / ".env.local"
DATA_API_URL = "https://data-api.polymarket.com/positions"
POLYGON_RPC = "https://polygon.drpc.org"
POLYGON_RPC_FALLBACKS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.drpc.org",
]
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
COLLATERAL_TOKEN_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Service settings
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "120"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8006"))
TRADE_LEDGER_PATH = os.environ.get(
    "TRADE_LEDGER_PATH",
    str(Path(__file__).resolve().parents[2] / "data" / "trade_ledger.json"),
)
SLEEP_BETWEEN_REDEMPTIONS = int(os.environ.get("SLEEP_BETWEEN_REDEMPTIONS", "5"))
TX_RECEIPT_TIMEOUT = int(os.environ.get("TX_RECEIPT_TIMEOUT", "45"))
GAS_PRICE_MULTIPLIER = 1.25  # boost gas price to avoid dropped txs
SIGNAL_ENGINE_HEALTH_URL = os.environ.get("SIGNAL_ENGINE_HEALTH_URL", "http://127.0.0.1:8003/health")
EXECUTION_ENGINE_HEALTH_URL = os.environ.get("EXECUTION_ENGINE_HEALTH_URL", "http://127.0.0.1:8004/health")
EXECUTION_ENGINE_STATUS_URL = os.environ.get("EXECUTION_ENGINE_STATUS_URL", "http://127.0.0.1:8004/status")
EXPECTED_LEDGER_VERSION = os.environ.get("EXPECTED_LEDGER_VERSION", "v15")
EXECUTION_ENGINE_LOG_PATH = os.environ.get(
    "EXECUTION_ENGINE_LOG_PATH",
    str(Path(__file__).resolve().parents[2] / "logs" / "execution-engine.log"),
)
# SIGNAL_VERSION_OVERRIDE was removed: version.json is the single source of truth

# ── Single source of truth: read from version.json written by btc-common build ──
_VERSION_JSON_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "version.json"
)
try:
    with open(_VERSION_JSON_PATH) as _vf:
        _VERSION_DATA = json.loads(_vf.read())
    DEFAULT_SIGNAL_VERSION = _VERSION_DATA.get("signal_version", "v14")
    logging.info("Loaded version from %s: %s", _VERSION_JSON_PATH, DEFAULT_SIGNAL_VERSION)
except Exception:
    DEFAULT_SIGNAL_VERSION = "v14"
    logging.warning("Could not read %s — falling back to '%s'", _VERSION_JSON_PATH, DEFAULT_SIGNAL_VERSION)

# ═══════════════════════════════════════════════════════════════
# ABIs (minimal)
# ═══════════════════════════════════════════════════════════════
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

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


# ═══════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════
@dataclass
class RedemptionResult:
    condition_id: str
    tx_hash: str
    status: str          # SUCCESS | FAILED | DRY_RUN
    gas_used: int
    block_number: int
    payout_usdc_total: Optional[float] = None


@dataclass
class TradeLedgerEntry:
    """Source-of-truth trade record from Polymarket's Data API."""
    redeemed_at: str          # ISO 8601 UTC
    slug: str
    title: str
    outcome: str              # "Up" or "Down"
    won: bool                 # profitable trade (cash_pnl > 0)
    settlement_won: bool      # binary settlement winner (curPrice ~= 1.0)
    size: float               # shares held
    avg_price: float          # average entry price
    initial_value: float      # cost basis
    current_value: float      # payout value
    cash_pnl: float           # absolute PnL from Polymarket
    percent_pnl: float        # % PnL from Polymarket
    cur_price: float          # 1.0 = won, 0.0 = lost
    settlement_price: Optional[float]  # on-chain settlement payout/share when available
    onchain_payout_usdc: Optional[float]  # allocated payout from tx receipt
    payout_source: str        # tx_receipt | polymarket_api
    condition_id: str
    tx_hash: Optional[str]
    tx_status: str            # SUCCESS | FAILED | DRY_RUN
    usdc_before: Optional[float]
    usdc_after: Optional[float]
    signal_version: str = "unknown"  # signal engine version tag
    execution_version: str = "unknown"  # execution engine version tag


# ═══════════════════════════════════════════════════════════════
# Trade ledger persistence
# ═══════════════════════════════════════════════════════════════
class TradeLedger:
    """Append-only JSON trade ledger — the definitive source of truth."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: List[dict] = self._load()

    def _load(self) -> List[dict]:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except (json.JSONDecodeError, IOError):
                logging.warning("Corrupt trade ledger at %s, starting fresh", self.path)
        return []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._entries, f, indent=2, default=str)
        tmp.rename(self.path)

    def has_condition(self, condition_id: str) -> bool:
        """Check if we already redeemed this condition (dedup)."""
        return any(
            e.get("condition_id") == condition_id and e.get("tx_status") == "SUCCESS"
            for e in self._entries
        )

    def append(self, entry: TradeLedgerEntry):
        self._entries.append(asdict(entry))
        self._save()

    @property
    def summary(self) -> dict:
        successful = [e for e in self._entries if e.get("tx_status") == "SUCCESS"]
        wins = [e for e in successful if e.get("won")]
        losses = [e for e in successful if not e.get("won")]
        settlement_wins = [
            e for e in successful
            if e.get("settlement_won", float(e.get("cur_price", 0)) >= 0.99)
        ]
        settlement_losses = [e for e in successful if e not in settlement_wins]
        total_pnl = sum(e.get("cash_pnl", 0) for e in successful)
        total_cost = sum(e.get("initial_value", 0) for e in successful)
        return {
            "total_redeemed": len(successful),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(successful) if successful else 0.0,
            "settlement_wins": len(settlement_wins),
            "settlement_losses": len(settlement_losses),
            "settlement_win_rate": len(settlement_wins) / len(successful) if successful else 0.0,
            "total_pnl": round(total_pnl, 6),
            "total_cost": round(total_cost, 6),
            "roi_pct": round((total_pnl / total_cost * 100) if total_cost > 0 else 0.0, 2),
        }

    @property
    def count(self) -> int:
        return len(self._entries)


# ═══════════════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health, ledger, and dashboard endpoints."""

    service_state: dict = {}
    _dashboard_html: str = ""

    @classmethod
    def _load_dashboard(cls):
        """Load dashboard HTML from file (cached)."""
        if not cls._dashboard_html:
            html_path = Path(__file__).parent / "dashboard.html"
            if html_path.exists():
                cls._dashboard_html = html_path.read_text()
            else:
                cls._dashboard_html = "<html><body><h1>dashboard.html not found</h1></body></html>"
        return cls._dashboard_html

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(self.__class__.service_state, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/ledger":
            ledger_path = Path(TRADE_LEDGER_PATH)
            if ledger_path.exists():
                body = ledger_path.read_bytes()
            else:
                body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" or self.path == "/dashboard":
            body = self._load_dashboard().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs


def start_health_server(port: int):
    """Start health endpoint in a background daemon thread."""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Health endpoint listening on :%d", port)


# ═══════════════════════════════════════════════════════════════
# Core service
# ═══════════════════════════════════════════════════════════════
class RedeemPositionsService:
    """Recurring service: fetch redeemable positions, redeem, log to ledger."""

    def __init__(self, dry_run: bool = False) -> None:
        load_dotenv(ENV_PATH)

        private_key = os.getenv("POLYGON_PRIVATE_KEY", "")
        if not private_key:
            raise ValueError("POLYGON_PRIVATE_KEY not set in " + ENV_PATH)
        if private_key.startswith("0x"):
            private_key = private_key[2:]

        self.account = Account.from_key(private_key)
        self.dry_run = dry_run

        self.rpc_endpoints = [
            os.environ.get("POLYGON_RPC", "").strip() or POLYGON_RPC,
            *POLYGON_RPC_FALLBACKS,
        ]
        self.rpc_endpoints = list(dict.fromkeys([u for u in self.rpc_endpoints if u]))
        self.rpc_index = 0
        self.w3 = self._connect_with_fallback()

        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
            abi=CONDITIONAL_TOKENS_ABI,
        )
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(COLLATERAL_TOKEN_ADDRESS),
            abi=ERC20_ABI,
        )
        self.ledger = TradeLedger(TRADE_LEDGER_PATH)
        self.last_run: Optional[str] = None
        self.last_redeemed: int = 0
        self.total_cycles: int = 0
        self.running = True

    def get_initial_deposit_usdc(self) -> Optional[float]:
        """Resolve initial deposited USDC for bankroll-level performance.

        Priority:
          1) explicit env override `INITIAL_USDC_DEPOSIT_USDC`
          2) earliest non-null `usdc_before` in ledger
        """
        override = os.environ.get("INITIAL_USDC_DEPOSIT_USDC")
        if override is not None:
            try:
                return float(override)
            except Exception:
                logging.warning(
                    "Invalid INITIAL_USDC_DEPOSIT_USDC='%s' (expected float)",
                    override,
                )

        rows = getattr(self.ledger, "_entries", [])
        if not rows:
            return None

        def _redeemed_at(row: dict) -> datetime:
            ts = row.get("redeemed_at")
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    pass
            return datetime.max.replace(tzinfo=timezone.utc)

        for row in sorted(rows, key=_redeemed_at):
            val = row.get("usdc_before")
            if val is None:
                continue
            try:
                return float(val)
            except Exception:
                continue

        return None

    @staticmethod
    def _extract_version(payload: Dict[str, Any], keys: List[str]) -> Optional[str]:
        for key in keys:
            version = payload.get(key)
            if isinstance(version, str) and version.strip():
                return version.strip()
        return None

    @staticmethod
    def _fetch_health_payload(url: str) -> Optional[Dict[str, Any]]:
        try:
            response = requests.get(url, timeout=2)
            if response.ok:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
        except Exception as exc:
            logging.debug("Could not fetch health payload from %s: %s", url, exc)
        return None

    def resolve_signal_version(self) -> str:
        """Resolve signal version from signal-engine health, else version.json default."""
        payload = self._fetch_health_payload(SIGNAL_ENGINE_HEALTH_URL)
        if payload:
            version = self._extract_version(payload, ["version"])
            if version:
                return version
        return DEFAULT_SIGNAL_VERSION

    def resolve_execution_version(self) -> str:
        """Resolve execution version from execution-engine health."""
        payload = self._fetch_health_payload(EXECUTION_ENGINE_HEALTH_URL)
        if payload:
            version = self._extract_version(payload, ["execution_version", "version"])
            if version:
                return version
        return "unknown"

    def _lookup_entry_price_from_engine(self, market_slug: str) -> Optional[float]:
        """Try to get the actual entry price from the execution engine's /status endpoint."""
        try:
            resp = requests.get(EXECUTION_ENGINE_STATUS_URL, timeout=3)
            if not resp.ok:
                return None
            data = resp.json()
            # Check recent closed positions for a matching market slug
            for pos in data.get("recent_closed", []):
                if pos.get("market", "") == market_slug and pos.get("entry_price"):
                    return float(pos["entry_price"])
            # Also check open positions (in case not yet closed in engine)
            for pos in data.get("open_positions", []):
                if pos.get("market", "") == market_slug and pos.get("entry_price"):
                    return float(pos["entry_price"])
        except Exception as exc:
            logging.debug("Could not look up entry price from execution engine: %s", exc)
        return None

    def _connect_with_fallback(self) -> Web3:
        """Connect to first healthy Polygon RPC endpoint in fallback chain."""
        last_error: Optional[Exception] = None

        for i, rpc_url in enumerate(self.rpc_endpoints):
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

                # Validate both transport + RPC method path
                if not w3.is_connected():
                    raise ConnectionError("not connected")
                _ = w3.eth.chain_id

                self.rpc_index = i
                logging.info("Connected to Polygon RPC: %s", rpc_url)
                return w3
            except Exception as exc:
                last_error = exc
                logging.warning("RPC endpoint unhealthy: %s (%s)", rpc_url, exc)

        raise ConnectionError(f"Failed to connect to any Polygon RPC endpoint: {last_error}")

    def _rotate_rpc(self):
        """Switch to next RPC endpoint in fallback chain."""
        if len(self.rpc_endpoints) <= 1:
            return
        self.rpc_index = (self.rpc_index + 1) % len(self.rpc_endpoints)
        next_rpc = self.rpc_endpoints[self.rpc_index]
        logging.warning("Switching Polygon RPC endpoint to %s", next_rpc)
        self.w3 = Web3(Web3.HTTPProvider(next_rpc, request_kwargs={"timeout": 20}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    def _rpc_call_with_retry(self, fn, delay_secs: int = 4, max_retries: int = 8):
        for attempt in range(1, max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                message = str(exc).lower()
                if "too many requests" in message or "rate limit" in message:
                    logging.warning("RPC rate limit (attempt %d), retrying in %ds", attempt, delay_secs)
                    time.sleep(delay_secs)
                    continue

                logging.warning(
                    "RPC call failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                self._rotate_rpc()
                time.sleep(delay_secs)

                if attempt == max_retries:
                    raise

    def get_redeemable_positions(self) -> List[Dict[str, Any]]:
        """Fetch redeemable positions from Polymarket Data API."""
        resp = requests.get(
            DATA_API_URL,
            params={"user": self.account.address, "redeemable": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, list) else []

    def _load_entry_price_fallbacks(self) -> Dict[str, Dict[str, float]]:
        """Parse execution-engine log for latest BUY price/size by market slug.

        This is a safety fallback when Polymarket positions API returns avgPrice=0
        or initialValue=0 for redeemable positions.
        """
        path = Path(EXECUTION_ENGINE_LOG_PATH)
        if not path.exists():
            return {}

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        entry_re = re.compile(
            r'ENTRY signal received.*market=Some\("([^"]+)"\)'
        )
        buy_re = re.compile(
            r'Placing BUY order .*price=([0-9]*\.?[0-9]+) .*size=([0-9]*\.?[0-9]+)'
        )

        fallbacks: Dict[str, Dict[str, float]] = {}
        current_slug: Optional[str] = None

        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    clean = ansi_re.sub("", line)
                    m_entry = entry_re.search(clean)
                    if m_entry:
                        current_slug = m_entry.group(1)
                        continue

                    m_buy = buy_re.search(clean)
                    if m_buy and current_slug:
                        fallbacks[current_slug] = {
                            "price": float(m_buy.group(1)),
                            "size": float(m_buy.group(2)),
                        }
        except Exception as exc:
            logging.warning("Failed parsing execution log fallbacks: %s", exc)

        return fallbacks

    def _load_activity_trade_fallbacks(self) -> Dict[str, Dict[str, float]]:
        """Load latest BUY TRADE cost basis by condition from Polymarket activity."""
        out: Dict[str, Dict[str, float]] = {}
        offset = 0
        limit = 500
        while True:
            try:
                resp = requests.get(
                    "https://data-api.polymarket.com/activity",
                    params={"user": self.account.address, "limit": limit, "offset": offset},
                    timeout=60,
                )
                resp.raise_for_status()
                batch = resp.json()
            except Exception as exc:
                logging.warning("Failed loading activity fallbacks: %s", exc)
                break

            if not isinstance(batch, list) or not batch:
                break

            for row in batch:
                if (row.get("type") or "").upper() != "TRADE":
                    continue
                if (row.get("side") or "").upper() != "BUY":
                    continue
                cid = (row.get("conditionId") or "").lower()
                size = float(row.get("size") or 0)
                usdc_size = float(row.get("usdcSize") or 0)
                api_price = float(row.get("price") or 0)
                ts = float(row.get("timestamp") or 0)
                if not cid or size <= 0 or usdc_size <= 0:
                    continue
                avg_price = api_price if api_price > 0 else (usdc_size / size)
                prev = out.get(cid)
                if prev is None or ts > prev.get("timestamp", 0):
                    out[cid] = {
                        "avg_price": avg_price,
                        "initial_value": usdc_size,
                        "timestamp": ts,
                    }

            if len(batch) < limit:
                break
            offset += limit

        return out

    def get_usdc_balance(self) -> Decimal:
        raw = self._rpc_call_with_retry(
            lambda: self.usdc_contract.functions.balanceOf(self.account.address).call()
        )
        return Decimal(raw) / Decimal(10**6)

    def _get_safe_nonce(self) -> int:
        """Get nonce, detecting and clearing any gap from dropped txs."""
        latest = self._rpc_call_with_retry(
            lambda: self.w3.eth.get_transaction_count(self.account.address, "latest")
        )
        pending = self._rpc_call_with_retry(
            lambda: self.w3.eth.get_transaction_count(self.account.address, "pending")
        )
        gap = pending - latest
        if gap > 0:
            logging.warning("Nonce gap detected: latest=%d pending=%d gap=%d. Clearing with filler txs...", latest, pending, gap)
            gas_price = int(self.w3.eth.gas_price * 1.5)
            chain_id = self.w3.eth.chain_id
            for nonce in range(latest, pending):
                tx = {
                    "from": self.account.address,
                    "to": self.account.address,
                    "value": 0,
                    "nonce": nonce,
                    "gas": 21000,
                    "gasPrice": gas_price,
                    "chainId": chain_id,
                }
                signed = self.w3.eth.account.sign_transaction(tx, self.account.key)
                self.w3.eth.send_raw_transaction(signed.raw_transaction)
                time.sleep(0.3)
            # Wait for last filler tx to confirm
            time.sleep(5)
            new_latest = self._rpc_call_with_retry(
                lambda: self.w3.eth.get_transaction_count(self.account.address, "latest")
            )
            logging.info("Nonce gap cleared: latest=%d", new_latest)
            return new_latest
        return latest

    def redeem_condition(self, condition_id_hex: str) -> RedemptionResult:
        """Redeem one condition ID on-chain."""
        if self.dry_run:
            fake_hash = self.w3.keccak(text=condition_id_hex).hex()
            return RedemptionResult(
                condition_id=condition_id_hex,
                tx_hash=fake_hash,
                status="DRY_RUN",
                gas_used=0,
                block_number=0,
                payout_usdc_total=None,
            )

        condition_bytes = self.w3.to_bytes(hexstr=condition_id_hex)
        parent_collection_id = bytes(32)
        index_sets = [1, 2]
        collateral_address = Web3.to_checksum_address(COLLATERAL_TOKEN_ADDRESS)

        try:
            gas_est = self._rpc_call_with_retry(
                lambda: self.ctf_contract.functions.redeemPositions(
                    collateral_address, parent_collection_id, condition_bytes, index_sets,
                ).estimate_gas({"from": self.account.address})
            )
            gas_limit = int(gas_est * 1.20)
        except Exception as e:
            logging.warning("Gas estimation failed for %s: %s. Using 400k.", condition_id_hex[:18], e)
            gas_limit = 400_000

        # Fetch gas price with 25% boost to avoid stuck/dropped txs on Polygon
        base_gas_price = self._rpc_call_with_retry(lambda: self.w3.eth.gas_price)
        boosted_gas_price = int(base_gas_price * GAS_PRICE_MULTIPLIER)

        # Use safe nonce (detects and clears gaps from dropped txs)
        nonce = self._get_safe_nonce()

        tx = self.ctf_contract.functions.redeemPositions(
            collateral_address, parent_collection_id, condition_bytes, index_sets,
        ).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": boosted_gas_price,
            "chainId": self._rpc_call_with_retry(lambda: self.w3.eth.chain_id),
        })
        logging.info("  → gas_price=%.1f gwei (base=%.1f, boost=%.0f%%)",
                     boosted_gas_price / 1e9, base_gas_price / 1e9,
                     (GAS_PRICE_MULTIPLIER - 1) * 100)

        signed_tx = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self._rpc_call_with_retry(
            lambda: self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        )
        logging.info("  → tx broadcast: %s", tx_hash.hex())

        # Wait for receipt with explicit timeout + retry on TimeExhausted
        receipt = self._wait_for_receipt(tx_hash)
        payout_usdc_total = self._extract_collateral_payout_usdc(receipt)

        return RedemptionResult(
            condition_id=condition_id_hex,
            tx_hash=tx_hash.hex(),
            status="SUCCESS" if receipt["status"] == 1 else "FAILED",
            gas_used=int(receipt["gasUsed"]),
            block_number=int(receipt["blockNumber"]),
            payout_usdc_total=payout_usdc_total,
        )

    def _extract_collateral_payout_usdc(self, receipt: dict) -> Optional[float]:
        """Extract net collateral-token payout to our wallet from a tx receipt."""
        try:
            transfer_topic = Web3.to_hex(
                Web3.keccak(text="Transfer(address,address,uint256)")
            ).lower()
            wallet = self.account.address.lower()
            collateral = Web3.to_checksum_address(COLLATERAL_TOKEN_ADDRESS).lower()
            total = Decimal("0")

            for log in receipt.get("logs", []):
                if str(log.get("address", "")).lower() != collateral:
                    continue

                topics = log.get("topics", [])
                if len(topics) < 3:
                    continue

                topic0 = Web3.to_hex(topics[0]).lower()
                if topic0 != transfer_topic:
                    continue

                to_addr = "0x" + Web3.to_hex(topics[2])[-40:].lower()
                if to_addr != wallet:
                    continue

                value_hex = Web3.to_hex(log.get("data", b"\x00"))
                value = int(value_hex, 16)
                total += Decimal(value) / Decimal(10**6)

            return float(total)
        except Exception as exc:
            logging.warning("Could not decode on-chain payout from receipt: %s", exc)
            return None

    def _wait_for_receipt(self, tx_hash, retries: int = 3):
        """Wait for tx receipt with shorter timeout and retries.

        Uses TX_RECEIPT_TIMEOUT (default 45s) per attempt. On TimeExhausted,
        retries up to `retries` times with a brief cooldown, since the tx may
        still be in the mempool waiting for inclusion.
        """
        for attempt in range(1, retries + 1):
            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=TX_RECEIPT_TIMEOUT
                )
                return receipt
            except Exception as exc:
                is_timeout = (
                    "not in the chain" in str(exc).lower()
                    or "TimeExhausted" in type(exc).__name__
                    or "Timeout" in type(exc).__name__
                )
                if is_timeout and attempt < retries:
                    logging.warning(
                        "  → receipt timeout (attempt %d/%d), retrying in 10s...",
                        attempt, retries,
                    )
                    time.sleep(10)
                    continue
                # Final attempt: one last direct check
                if is_timeout:
                    logging.warning("  → receipt timeout (attempt %d/%d), final check...", attempt, retries)
                    time.sleep(5)
                    try:
                        receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                        if receipt is not None:
                            logging.info("  → receipt found on final check!")
                            return receipt
                    except Exception:
                        pass
                raise

    def run_cycle(self) -> int:
        """Execute one redemption cycle. Returns count of newly redeemed."""
        now = datetime.now(timezone.utc).isoformat()
        self.total_cycles += 1

        positions = self.get_redeemable_positions()
        if not positions:
            logging.info("No redeemable positions found")
            self.last_run = now
            self.last_redeemed = 0
            return 0

        # Group positions by conditionId, keeping full position data
        by_condition: Dict[str, List[Dict]] = {}
        for pos in positions:
            cid = pos.get("conditionId", "")
            if not cid:
                continue
            by_condition.setdefault(cid, []).append(pos)

        # Skip already-redeemed conditions
        new_conditions = {
            cid: rows for cid, rows in by_condition.items()
            if not self.ledger.has_condition(cid)
        }

        if not new_conditions:
            logging.info("All %d redeemable positions already in ledger, nothing to do", len(positions))
            self.last_run = now
            self.last_redeemed = 0
            return 0

        logging.info("Found %d new conditions to redeem (%d already done)",
                      len(new_conditions), len(by_condition) - len(new_conditions))

        signal_version = self.resolve_signal_version()
        execution_version = self.resolve_execution_version()
        logging.info("Using signal version tag for this cycle: %s", signal_version)
        logging.info("Using execution version tag for this cycle: %s", execution_version)
        if EXPECTED_LEDGER_VERSION and execution_version != EXPECTED_LEDGER_VERSION:
            logging.warning(
                "Resolved execution version '%s' does not match EXPECTED_LEDGER_VERSION '%s'",
                execution_version,
                EXPECTED_LEDGER_VERSION,
            )

        balance_before = float(self.get_usdc_balance())
        redeemed_count = 0
        entry_price_fallbacks = self._load_entry_price_fallbacks()
        activity_fallbacks = self._load_activity_trade_fallbacks()

        for idx, (condition_id, pos_rows) in enumerate(new_conditions.items(), start=1):
            total_size = sum(float(pos.get("size", 0)) for pos in pos_rows)

            # Log each position under this condition as source of truth
            for pos in pos_rows:
                slug = pos.get("slug", "unknown")
                outcome = pos.get("outcome", "?")
                cur_price = float(pos.get("curPrice", 0))
                cash_pnl = float(pos.get("cashPnl", 0))
                size = float(pos.get("size", 0))
                avg_price = float(pos.get("avgPrice", 0))

                cid_key = (condition_id or "").lower()
                if avg_price <= 0 and size > 0:
                    afb = activity_fallbacks.get(cid_key)
                    if afb and afb.get("avg_price", 0) > 0:
                        avg_price = float(afb["avg_price"])
                        logging.warning(
                            "Recovered avgPrice from activity for %s: %.4f",
                            slug,
                            avg_price,
                        )

                if avg_price <= 0 and size > 0:
                    fb = entry_price_fallbacks.get(slug)
                    if fb and fb.get("price", 0) > 0:
                        avg_price = float(fb["price"])
                        logging.warning(
                            "Recovered avgPrice from execution log for %s: %.4f",
                            slug,
                            avg_price,
                        )

                settlement_won = cur_price >= 0.99  # binary settlement winner
                won = cash_pnl > 0  # profitability-based win for reporting

                result_emoji = "✅" if won else "❌"
                logging.info(
                    "%s [%d/%d] %s → %s (won=%s settlement_won=%s) size=%.4f avg=%.4f pnl=$%.4f (%.1f%%)",
                    result_emoji, idx, len(new_conditions),
                    slug, outcome, won, settlement_won,
                    size,
                    avg_price,
                    cash_pnl,
                    float(pos.get("percentPnl", 0)),
                )

            # Redeem on-chain
            try:
                result = self.redeem_condition(condition_id)
                logging.info("  → %s tx=%s gas=%s", result.status, result.tx_hash[:16], result.gas_used)
            except Exception as exc:
                logging.exception("  → FAILED to redeem %s: %s", condition_id[:18], exc)
                result = RedemptionResult(
                    condition_id=condition_id,
                    tx_hash="",
                    status="FAILED",
                    gas_used=0,
                    block_number=0,
                    payout_usdc_total=None,
                )

            # Check USDC balance after this redemption
            try:
                balance_after = float(self.get_usdc_balance())
            except Exception:
                balance_after = None

            # Write each position as a ledger entry
            for pos in pos_rows:
                cur_price = float(pos.get("curPrice", 0))
                size = float(pos.get("size", 0))
                avg_price = float(pos.get("avgPrice", 0))
                initial_value = float(pos.get("initialValue", 0))
                current_value = float(pos.get("currentValue", 0))
                cash_pnl = float(pos.get("cashPnl", 0))
                percent_pnl = float(pos.get("percentPnl", 0))
                slug = pos.get("slug", "unknown")

                # ── Fix $0 cost bug: Polymarket API sometimes returns
                # avgPrice=0 / initialValue=0.  Fall back to execution
                # engine's actual entry price when this happens. ──
                if avg_price == 0 or initial_value == 0:
                    engine_price = self._lookup_entry_price_from_engine(slug)
                    if engine_price and engine_price > 0:
                        logging.warning(
                            "Polymarket API returned avg_price=%.4f initial_value=%.4f for %s — "
                            "using execution engine entry_price=%.4f instead",
                            avg_price, initial_value, slug, engine_price,
                        )
                        avg_price = engine_price
                        initial_value = avg_price * size
                    elif avg_price > 0 and initial_value == 0:
                        initial_value = avg_price * size
                        logging.warning(
                            "initialValue=0 for %s — computed from avg_price*size = %.4f",
                            slug, initial_value,
                        )
                    elif initial_value > 0 and avg_price == 0 and size > 0:
                        avg_price = initial_value / size
                        logging.warning(
                            "avgPrice=0 for %s — computed from initialValue/size = %.4f",
                            slug, avg_price,
                        )
                    else:
                        logging.error(
                            "CANNOT RECOVER cost data for %s: avg_price=%.4f initial_value=%.4f "
                            "size=%.4f — execution engine lookup also failed",
                            slug, avg_price, initial_value, size,
                        )

                settlement_price = cur_price
                onchain_payout_usdc: Optional[float] = None
                payout_source = "polymarket_api"

                slug = pos.get("slug", "unknown")
                cid_key = (condition_id or "").lower()
                if (avg_price <= 0 or initial_value <= 0) and size > 0:
                    afb = activity_fallbacks.get(cid_key)
                    if afb:
                        if avg_price <= 0 and afb.get("avg_price", 0) > 0:
                            avg_price = float(afb["avg_price"])
                        if initial_value <= 0 and afb.get("initial_value", 0) > 0:
                            initial_value = float(afb["initial_value"])
                        logging.warning(
                            "Recovered cost basis from activity for %s: avgPrice=%.4f initial=%.4f",
                            slug,
                            avg_price,
                            initial_value,
                        )

                if avg_price <= 0 and size > 0:
                    fb = entry_price_fallbacks.get(slug)
                    if fb and fb.get("price", 0) > 0:
                        avg_price = float(fb["price"])
                        logging.warning(
                            "Recovered cost basis from execution log for %s: avgPrice=%.4f",
                            slug,
                            avg_price,
                        )

                if initial_value <= 0 and avg_price > 0 and size > 0:
                    initial_value = avg_price * size
                    cash_pnl = current_value - initial_value
                    percent_pnl = (cash_pnl / initial_value * 100) if initial_value > 0 else 0.0

                if (
                    result.status == "SUCCESS"
                    and result.payout_usdc_total is not None
                    and total_size > 0
                    and size > 0
                ):
                    onchain_payout_usdc = float(result.payout_usdc_total) * (size / total_size)
                    current_value = onchain_payout_usdc
                    cash_pnl = current_value - initial_value
                    percent_pnl = (cash_pnl / initial_value * 100) if initial_value > 0 else 0.0
                    settlement_price = current_value / size
                    payout_source = "tx_receipt"

                settlement_won = settlement_price >= 0.99
                entry = TradeLedgerEntry(
                    redeemed_at=now,
                    slug=slug,
                    title=pos.get("title", ""),
                    outcome=pos.get("outcome", "?"),
                    won=cash_pnl > 0,
                    settlement_won=settlement_won,
                    size=size,
                    avg_price=avg_price,
                    initial_value=initial_value,
                    current_value=current_value,
                    cash_pnl=cash_pnl,
                    percent_pnl=percent_pnl,
                    cur_price=cur_price,
                    settlement_price=settlement_price,
                    onchain_payout_usdc=onchain_payout_usdc,
                    payout_source=payout_source,
                    condition_id=condition_id,
                    tx_hash=result.tx_hash if result.tx_hash else None,
                    tx_status=result.status,
                    usdc_before=balance_before,
                    usdc_after=balance_after,
                    signal_version=signal_version,
                    execution_version=execution_version,
                )
                self.ledger.append(entry)

            if result.status == "SUCCESS":
                redeemed_count += 1
                if balance_after is not None:
                    balance_before = balance_after  # cascade for next

            # Sleep between redemptions to avoid nonce issues
            if not self.dry_run and idx < len(new_conditions):
                time.sleep(SLEEP_BETWEEN_REDEMPTIONS)

        # Final balance
        try:
            final_balance = float(self.get_usdc_balance())
        except Exception:
            final_balance = balance_before

        summary = self.ledger.summary
        logging.info("═" * 70)
        logging.info("REDEMPTION CYCLE COMPLETE")
        logging.info("═" * 70)
        logging.info("  Redeemed this cycle:  %d conditions", redeemed_count)
        logging.info("  Ledger total:         %d trades", summary["total_redeemed"])
        logging.info("  Win/Loss:             %d / %d (%.1f%%)",
                      summary["wins"], summary["losses"], summary["win_rate"] * 100)
        logging.info("  Total PnL:            $%.4f", summary["total_pnl"])
        logging.info("  ROI:                  %.2f%%", summary["roi_pct"])
        logging.info("  USDC.e balance:       $%.6f", final_balance)
        if self.dry_run:
            logging.info("  ⚠️  DRY RUN — no on-chain transactions submitted")
        logging.info("═" * 70)

        self.last_run = now
        self.last_redeemed = redeemed_count
        return redeemed_count

    def update_health_state(self):
        """Push current state to the health endpoint."""
        summary = self.ledger.summary
        try:
            current_wallet_usdc = float(self.get_usdc_balance())
        except Exception as exc:
            logging.debug("Could not fetch current wallet USDC for health state: %s", exc)
            current_wallet_usdc = None

        initial_deposit_usdc = self.get_initial_deposit_usdc()
        bankroll_pnl = None
        bankroll_roi_pct = None
        if initial_deposit_usdc is not None and current_wallet_usdc is not None:
            bankroll_pnl = current_wallet_usdc - initial_deposit_usdc
            bankroll_roi_pct = (bankroll_pnl / initial_deposit_usdc * 100.0) if initial_deposit_usdc > 0 else 0.0

        HealthHandler.service_state = {
            "service": "redeem-positions",
            "status": "ok",
            "dry_run": self.dry_run,
            "wallet": self.account.address,
            "poll_interval_secs": POLL_INTERVAL_SECS,
            "last_run": self.last_run,
            "last_redeemed": self.last_redeemed,
            "total_cycles": self.total_cycles,
            "ledger": summary,
            "bankroll": {
                "initial_deposit_usdc": round(initial_deposit_usdc, 6) if initial_deposit_usdc is not None else None,
                "current_wallet_usdc": round(current_wallet_usdc, 6) if current_wallet_usdc is not None else None,
                "real_pnl_usdc": round(bankroll_pnl, 6) if bankroll_pnl is not None else None,
                "real_roi_pct": round(bankroll_roi_pct, 4) if bankroll_roi_pct is not None else None,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def run_forever(self):
        """Main service loop."""
        logging.info("═" * 70)
        logging.info("POLYMARKET REDEMPTION SERVICE (recurring)")
        logging.info("═" * 70)
        logging.info("  Wallet:       %s", self.account.address)
        logging.info("  Poll interval: %ds", POLL_INTERVAL_SECS)
        logging.info("  Trade ledger: %s", TRADE_LEDGER_PATH)
        logging.info("  Health port:  %d", HEALTH_PORT)
        logging.info("  Dry run:      %s", self.dry_run)
        logging.info("  Ledger has:   %d existing entries", self.ledger.count)
        logging.info("═" * 70)

        # Publish initial health state before serving /health to avoid empty payload races
        self.update_health_state()
        start_health_server(HEALTH_PORT)

        while self.running:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                break
            except Exception:
                logging.exception("Error in redemption cycle")

            self.update_health_state()

            logging.info("Next poll in %ds...", POLL_INTERVAL_SECS)
            # Interruptible sleep
            for _ in range(POLL_INTERVAL_SECS):
                if not self.running:
                    break
                time.sleep(1)

        logging.info("Service stopped")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Position Redemption Service")
    parser.add_argument("--dry-run", action="store_true", help="Don't submit on-chain transactions")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()
    service = RedeemPositionsService(dry_run=args.dry_run)

    # Graceful shutdown
    def handle_signal(signum, frame):
        logging.info("Received signal %d, shutting down...", signum)
        service.running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if args.once:
        service.run_cycle()
        return 0

    service.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
