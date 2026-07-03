#!/usr/bin/env python3
"""
Get Polymarket deposit address
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
    
    print("\nLooking for deposit-related methods...")
    deposit_methods = [m for m in dir(client) if not m.startswith('_')]
    print("\nAll public methods:")
    for method in sorted(deposit_methods):
        print(f"  - {method}")

except Exception as e:
    import traceback
    traceback.print_exc()
