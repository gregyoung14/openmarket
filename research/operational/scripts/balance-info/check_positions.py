#!/usr/bin/env python3
"""
Check current positions and balances
"""
import json
import sys
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.wallet_env import get_private_key  # noqa: E402

private_key = get_private_key()

client = ClobClient(
    host="https://clob.polymarket.com",
    key=private_key,
    chain_id=POLYGON,
    signature_type=0,
    funder=None
)

print(f"Wallet: {client.get_address()}")
print("\nDeriving API credentials...")
api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)

print("\n" + "=" * 60)
print("CHECKING POSITIONS")
print("=" * 60)

try:
    # Get open orders
    print("\nOpen orders:")
    orders = client.get_orders()
    print(json.dumps(orders, indent=2))
    
    # Get balance
    print("\nBalance:")
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
    balance = client.get_balance_allowance(params=params)
    print(json.dumps(balance, indent=2))
    
except Exception as e:
    print(f"Error: {e}")
