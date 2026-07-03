#!/usr/bin/env python3
"""
Check Polymarket balance after deposit
"""
import sys
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.wallet_env import get_polymarket_creds, get_private_key  # noqa: E402

private_key = get_private_key()

print("=" * 60)
print("CHECKING POLYMARKET BALANCE")
print("=" * 60)

try:
    print("\nInitializing ClobClient...")
    creds = get_polymarket_creds()
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=POLYGON,
        creds=creds,
        signature_type=2
    )
    
    print("Checking balance...")
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=2
    )
    
    for attempt in range(1):
        balance_info = client.get_balance_allowance(params=params)
        balance_usdc = float(balance_info.get('balance', '0')) / 10**6
        
        print(f"\nAttempt {attempt + 1}/3:")
        print(f"Balance: {balance_usdc} USDC")
        
        if balance_usdc > 0:
            print("\n" + "=" * 60)
            print("✅ DEPOSIT CONFIRMED!")
            print("=" * 60)
            print(f"\nYour Polymarket balance: {balance_usdc} USDC")
            print("\nYou can now trade on Polymarket! 🚀")
            print("=" * 60)
            break
        else:
            if attempt < 2:
                #print("Waiting 30 seconds for deposit to process...")
                #time.sleep(30)
                pass
            else:
                print("\n⏳ Deposit still processing...")
                print("This can take up to 5 minutes.")
                print("Run this script again in a few minutes to check.")

except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"\n❌ Error: {e}")
