#!/usr/bin/env python3
"""Shared wallet utilities for Polygon/Polymarket maintenance scripts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from eth_account import Account
from py_clob_client.clob_types import ApiCreds
from web3 import Web3


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILES = (
    REPO_ROOT / ".env.local",
    REPO_ROOT / "wallet-gen" / ".env.wallet",
)

POLYGON_CHAIN_ID = 137
POLYGON_RPC_URLS = (
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
)

BRIDGED_USDCE = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
NATIVE_USDC = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
UNISWAP_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
UNISWAP_QUOTER = Web3.to_checksum_address("0x61fFE014bA17989E743c5F6cB21bF9697530B21e")
USDC_DECIMALS = 6

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


def load_env_files(paths: Iterable[Path] = DEFAULT_ENV_FILES) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_private_key() -> str:
    load_env_files()
    private_key = os.environ.get("POLYGON_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("Set POLYGON_PRIVATE_KEY or PRIVATE_KEY in the environment or ignored env files")
    private_key = private_key.strip()
    return private_key if private_key.startswith("0x") else f"0x{private_key}"


def get_account():
    return Account.from_key(get_private_key())


def get_polymarket_creds() -> ApiCreds:
    load_env_files()
    missing = [
        key
        for key in ("POLYMARKET_API_KEY", "POLYMARKET_SECRET", "POLYMARKET_PASSPHRASE")
        if not os.environ.get(key)
    ]
    if missing:
        raise RuntimeError(f"Missing Polymarket API credential(s): {', '.join(missing)}")
    return ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_SECRET"],
        api_passphrase=os.environ["POLYMARKET_PASSPHRASE"],
    )


def connect_polygon(rpc_url: Optional[str] = None) -> Web3:
    urls = (rpc_url,) if rpc_url else POLYGON_RPC_URLS
    for url in urls:
        if not url:
            continue
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
        try:
            if w3.is_connected() and w3.eth.chain_id == POLYGON_CHAIN_ID:
                return w3
        except Exception:
            continue
    raise RuntimeError("Could not connect to Polygon")


def format_token(amount: int, decimals: int = USDC_DECIMALS) -> str:
    whole = amount / (10**decimals)
    return f"{whole:.6f}"
