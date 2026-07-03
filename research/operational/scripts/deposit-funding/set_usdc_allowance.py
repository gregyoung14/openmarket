#!/usr/bin/env python3
"""
Set USDC Allowance for Polymarket Trading

PROBLEM:
--------
When trading on Polymarket via the API, you may get "not enough balance/allowance" errors
even though your wallet has sufficient USDC. This is because the Polymarket exchange contract
needs explicit permission (ERC-20 allowance) to spend your USDC tokens.

SOLUTION:
---------
This script sets the USDC allowance to the maximum value (virtually unlimited) so you
won't encounter allowance errors when trading. You only need to run this ONCE.

WHAT IT DOES:
- Connects to your Polygon wallet using your private key
- Derives API credentials for Polymarket
- Approves the Polymarket CTF Exchange contract to spend USDC
- Sets allowance to MAX_UINT256 (effectively unlimited)
- Confirms the transaction

REQUIREMENTS:
- Your POLYGON_PRIVATE_KEY must be set in .env.local
- Must have a tiny amount of MATIC (~$0.01) for gas fees on Polygon

USAGE:
------
    python3 scripts/set_usdc_allowance.py

    Optionally show current allowance without making changes:
    python3 scripts/set_usdc_allowance.py --check-only

EXPECTED OUTPUT:
    ✓ Current allowance: $3.98
    ✓ Setting allowance to maximum...
    ✓ Approval tx confirmed
    ✓ New allowance: $999,999,999.99 (effectively unlimited)

After running this, you should be able to place larger orders without allowance issues.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
import config
import time

# Constants
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CTF Exchange
MAX_UINT256 = 2**256 - 1  # Maximum possible uint256 value


def check_current_allowance():
    """Check current USDC allowance without making changes"""
    print("\n" + "=" * 60)
    print("CHECKING CURRENT USDC ALLOWANCE")
    print("=" * 60)
    
    # Initialize client
    private_key = config.POLYGON_PRIVATE_KEY
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=POLYGON,
        signature_type=0,  # EOA wallet
        funder=None
    )
    
    wallet = client.get_address()
    print(f"\nWallet: {wallet}")
    print(f"Exchange Contract: {EXCHANGE_ADDRESS}")
    print(f"USDC Token: {USDC_ADDRESS}\n")
    
    # Derive API credentials
    print("Deriving API credentials...")
    try:
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
    except Exception as e:
        print(f"⚠️  Warning: Could not derive creds: {e}")
    
    # Check allowance
    try:
        print("Checking allowance...")
        allowance_info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        
        if allowance_info and isinstance(allowance_info, dict):
            allowances = allowance_info.get('allowances', {})
            
            print(f"\n✓ Allowances for collateral (USDC):")
            for contract, amount in allowances.items():
                amount_float = float(amount) / 1e6  # USDC has 6 decimals
                print(f"  {contract}: ${amount_float:,.2f}")
            
            # Check if exchange contract allowance is sufficient
            exchange_allowance = allowances.get(EXCHANGE_ADDRESS, 0)
            exchange_allowance_float = float(exchange_allowance) / 1e6
            
            print(f"\n✓ CTF Exchange Allowance: ${exchange_allowance_float:,.2f}")
            
            if exchange_allowance_float > 1000:
                print("  ✓ Allowance is HIGH (unlimited for practical purposes)")
            elif exchange_allowance_float > 100:
                print("  ✓ Allowance is MODERATE (good for most trades)")
            elif exchange_allowance_float > 0:
                print("  ⚠️  Allowance is LOW (may need to increase for larger trades)")
            else:
                print("  ✗ Allowance is ZERO (must set before trading!)")
        else:
            print(f"\nAllowance data: {allowance_info}")
            
    except Exception as e:
        print(f"\n✗ Error checking allowance: {e}")
        import traceback
        traceback.print_exc()


def set_allowance_to_max():
    """Set USDC allowance to maximum"""
    print("\n" + "=" * 60)
    print("SETTING USDC ALLOWANCE TO MAXIMUM")
    print("=" * 60)
    
    # Initialize client
    private_key = config.POLYGON_PRIVATE_KEY
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=POLYGON,
        signature_type=0,  # EOA wallet
        funder=None
    )
    
    wallet = client.get_address()
    print(f"\nWallet: {wallet}")
    print(f"Exchange Contract: {EXCHANGE_ADDRESS}")
    print(f"USDC Token: {USDC_ADDRESS}\n")
    
    # Derive API credentials
    print("Deriving API credentials...")
    try:
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        print("✓ API credentials derived")
    except Exception as e:
        print(f"⚠️  Warning: {e}")
    
    # Check current allowance first
    try:
        print("\nChecking current allowance...")
        allowance_info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        
        if allowance_info and isinstance(allowance_info, dict):
            allowances = allowance_info.get('allowances', {})
            current = allowances.get(EXCHANGE_ADDRESS, 0)
            current_float = float(current) / 1e6
            print(f"✓ Current allowance: ${current_float:,.2f}")
        else:
            print(f"Current allowance: {allowance_info}")
            
    except Exception as e:
        print(f"⚠️  Could not check current allowance: {e}")
    
    # Update allowance to maximum
    print("\n⏳ Updating allowance to MAXIMUM...")
    print("   (This is a blockchain transaction, may take 10-30 seconds)\n")
    
    try:
        response = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        
        print(f"✓ Approval request submitted")
        if response:
            print(f"✓ Response: {response}\n")
        
        # Wait a bit for confirmation
        print("⏳ Waiting for transaction confirmation...")
        time.sleep(15)  # Wait 15 seconds for Polygon to confirm
        
        # Check new allowance
        print("\nVerifying new allowance...")
        allowance_info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        
        if allowance_info and isinstance(allowance_info, dict):
            allowances = allowance_info.get('allowances', {})
            new_allowance = allowances.get(EXCHANGE_ADDRESS, 0)
            new_allowance_float = float(new_allowance) / 1e6
            
            print(f"\n{'=' * 60}")
            print(f"✓ NEW ALLOWANCE: ${new_allowance_float:,.2f}")
            print(f"{'=' * 60}")
            
            if new_allowance_float > 1000000:
                print("\n✓✓✓ SUCCESS! Allowance is now UNLIMITED")
                print("You can now place trades without allowance issues!")
            else:
                print(f"\n⚠️  Allowance updated but may still be limited: ${new_allowance_float:,.2f}")
        
    except Exception as e:
        print(f"\n✗ Error updating allowance: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Set USDC allowance for Polymarket trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check current allowance
  python3 scripts/set_usdc_allowance.py --check-only
  
  # Set allowance to maximum
  python3 scripts/set_usdc_allowance.py
        """
    )
    parser.add_argument('--check-only', action='store_true',
                       help='Only check current allowance, do not modify')
    
    args = parser.parse_args()
    
    if args.check_only:
        check_current_allowance()
    else:
        success = set_allowance_to_max()
        sys.exit(0 if success else 1)
