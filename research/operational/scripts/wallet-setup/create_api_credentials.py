#!/usr/bin/env python3
"""
Test API credentials and signature
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds

POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

print("Testing API credentials...")

# Try without API creds first (for public endpoints)
try:
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=POLYGON_PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=2
    )
    
    print(f"✅ Client initialized")
    print(f"Wallet: {client.get_address()}")
    
    # Try to create new API credentials
    print("\nCreating new API credentials...")
    creds = client.create_api_key()
    
    print("\n" + "=" * 60)
    print("NEW API CREDENTIALS")
    print("=" * 60)
    print(f"API Key: {creds.api_key}")
    print(f"Secret: {creds.api_secret}")
    print(f"Passphrase: {creds.api_passphrase}")
    print("\n⚠️  Save these to your .env.local file!")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
