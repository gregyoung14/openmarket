#!/usr/bin/env python3
"""
Deposit USDC into Polymarket using py_clob_client
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

# Credentials
POLYMARKET_API_KEY = "REPLACE_WITH_POLYMARKET_API_KEY"
POLYMARKET_SECRET = "REPLACE_WITH_POLYMARKET_SECRET"
POLYMARKET_PASSPHRASE = "REPLACE_WITH_POLYMARKET_PASSPHRASE"
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

try:
    print("=" * 60)
    print("POLYMARKET DEPOSIT VIA CLOB CLIENT")
    print("=" * 60)
    
    print("\nInitializing ClobClient...")
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
    
    print("\nChecking current balance...")
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=2
    )
    
    balance_before = client.get_balance_allowance(params=params)
    print(f"Balance before: {float(balance_before.get('balance', '0')) / 10**6} USDC")
    
    print("\nUpdating balance allowance (this will sync on-chain balance to Polymarket)...")
    result = client.update_balance_allowance(params=params)
    
    print(f"Update result: {result}")
    
    print("\nChecking balance after update...")
    balance_after = client.get_balance_allowance(params=params)
    balance_usdc = float(balance_after.get('balance', '0')) / 10**6
    print(f"Balance after: {balance_usdc} USDC")
    
    if balance_usdc > 0:
        print("\n" + "=" * 60)
        print("✅ SUCCESS! Your USDC is now available in Polymarket!")
        print(f"Balance: {balance_usdc} USDC")
        print("=" * 60)
    else:
        print("\n⚠️  Balance still 0. You may need to manually deposit via the Polymarket UI.")
        print("Or the on-chain deposit transaction needs to be done first.")

except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"\n❌ Error: {e}")
