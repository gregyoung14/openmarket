#!/usr/bin/env python3
"""
Script to derive Polymarket L2 API credentials from a Polygon Private Key.
Useful for populating .env.local with permanent credentials.
"""
import sys
import os
import asyncio
from pathlib import Path

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Error importing dependencies: {e}")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

# Load env vars
load_dotenv(Path(__file__).parent.parent / '.env.local')


def main():
    private_key = os.getenv("POLYGON_PRIVATE_KEY")
    
    if not private_key:
        print("❌ Error: POLYGON_PRIVATE_KEY not found in .env.local")
        print("Please add it first:")
        print("echo 'POLYGON_PRIVATE_KEY=your_key_here' >> .env.local")
        sys.exit(1)

    # Clean key just in case
    if private_key.startswith("0x"):
        private_key = private_key[2:]

    print(f"🔑 Using Private Key: {private_key[:6]}...{private_key[-4:]}")
    print("🔄 Connecting to Polymarket CLOB to derive credentials...")

    try:
        # Initialize client with just L1 key
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=POLYGON
        )

        # Derive credentials
        # create_or_derive_api_creds() checks if keys exist, if so returns them, else creates new
        creds = client.create_or_derive_api_creds()
        
        print("\n✅ Success! Here are your L2 Credentials:")
        print("="*60)
        print(f"POLYMARKET_API_KEY={creds.api_key}")
        print(f"POLYMARKET_SECRET={creds.api_secret}")
        print(f"POLYMARKET_PASSPHRASE={creds.api_passphrase}")
        print("="*60)
        print("\n📝 Copy the lines above into your .env.local file for permanent authentication.")

    except Exception as e:
        print(f"\n❌ Error deriving credentials: {str(e)}")
        print("Troubleshooting:")
        print("1. Check if Private Key is correct")
        print("2. Ensure you are not in a Geo-blocked region (US)")
        print("3. Check internet connection")

if __name__ == "__main__":
    main()
