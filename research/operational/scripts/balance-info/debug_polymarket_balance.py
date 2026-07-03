#!/usr/bin/env python3
"""
Debug Polymarket balance check - see full API response
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
import json

# Credentials
POLYMARKET_API_KEY = "REPLACE_WITH_POLYMARKET_API_KEY"
POLYMARKET_SECRET = "REPLACE_WITH_POLYMARKET_SECRET"
POLYMARKET_PASSPHRASE = "REPLACE_WITH_POLYMARKET_PASSPHRASE"
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

print("Initializing ClobClient...")
creds = ApiCreds(
    api_key=POLYMARKET_API_KEY,
    api_secret=POLYMARKET_SECRET,
    api_passphrase=POLYMARKET_PASSPHRASE
)

client = ClobClient(
    host="https://clob.polymarket.com",
    key=POLYGON_PRIVATE_KEY,
    chain_id=POLYGON,
    creds=creds,
    signature_type=2
)

print(f"Wallet address: {client.get_address()}")

params = BalanceAllowanceParams(
    asset_type=AssetType.COLLATERAL,
    signature_type=2
)

print("\nGetting balance allowance...")
balance_info = client.get_balance_allowance(params=params)

print("\nFull API Response:")
print(json.dumps(balance_info, indent=2))

print("\nParsed values:")
print(f"Balance: {balance_info.get('balance', 'N/A')}")
print(f"Allowance: {balance_info.get('allowance', 'N/A')}")

if balance_info.get('balance'):
    balance_usdc = float(balance_info['balance']) / 10**6
    print(f"Balance in USDC: {balance_usdc}")
