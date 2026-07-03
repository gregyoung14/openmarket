#!/usr/bin/env python3
"""
Get Polymarket exchange/deposit addresses
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds

# Credentials
POLYMARKET_API_KEY = "REPLACE_WITH_POLYMARKET_API_KEY"
POLYMARKET_SECRET = "REPLACE_WITH_POLYMARKET_SECRET"
POLYMARKET_PASSPHRASE = "REPLACE_WITH_POLYMARKET_PASSPHRASE"
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

try:
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
    
    print("\n" + "=" * 60)
    print("POLYMARKET ADDRESSES")
    print("=" * 60)
    
    print(f"\nYour wallet address: {client.get_address()}")
    print(f"Collateral address: {client.get_collateral_address()}")
    print(f"Conditional address: {client.get_conditional_address()}")
    print(f"Exchange address: {client.get_exchange_address()}")
    
    print("\n" + "=" * 60)
    print("INSTRUCTIONS")
    print("=" * 60)
    print("\nTo deposit USDC into Polymarket:")
    print("1. Go to https://polymarket.com/cash")
    print("2. Click 'Deposit'")
    print("3. Select 'Exchange/Other' → 'Polygon'")
    print("4. They will show you a unique deposit address")
    print("5. Send your USDC to that address")
    print("\nAlternatively, if the exchange address above is your deposit address,")
    print("you can send USDC directly to it from your wallet.")

except Exception as e:
    import traceback
    traceback.print_exc()
