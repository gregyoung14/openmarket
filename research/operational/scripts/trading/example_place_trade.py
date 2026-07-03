#!/usr/bin/env python3
"""
Example: Place a trade on Polymarket
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds
import json

# Your credentials
POLYMARKET_API_KEY = "REPLACE_WITH_POLYMARKET_API_KEY"
POLYMARKET_SECRET = "REPLACE_WITH_POLYMARKET_SECRET"
POLYMARKET_PASSPHRASE = "REPLACE_WITH_POLYMARKET_PASSPHRASE"
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

print("=" * 60)
print("POLYMARKET TRADING EXAMPLE")
print("=" * 60)

# Initialize client
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

print(f"\nConnected as: {client.get_address()}")

# Example 1: Get popular markets
print("\n" + "=" * 60)
print("FETCHING POPULAR MARKETS")
print("=" * 60)

try:
    # Note: You might need to use the Gamma API for market data
    # The CLOB client is primarily for trading
    import requests
    
    # Get markets from Gamma API
    response = requests.get("https://gamma-api.polymarket.com/markets", params={
        "limit": 5,
        "active": True
    })
    
    if response.status_code == 200:
        markets = response.json()
        
        print(f"\nFound {len(markets)} active markets:")
        for i, market in enumerate(markets[:3], 1):
            print(f"\n{i}. {market.get('question', 'N/A')}")
            print(f"   Market ID: {market.get('condition_id', 'N/A')}")
            print(f"   Outcomes: {', '.join([o.get('outcome', '') for o in market.get('outcomes', [])])}")
            
            # Get token IDs for trading
            for outcome in market.get('outcomes', []):
                print(f"   - {outcome.get('outcome')}: Token ID {outcome.get('token_id', 'N/A')}")
    else:
        print(f"Failed to fetch markets: {response.status_code}")
        
except Exception as e:
    print(f"Error: {e}")

# Example 2: How to place an order (uncomment to execute)
print("\n" + "=" * 60)
print("HOW TO PLACE AN ORDER")
print("=" * 60)
print("""
To place an order:

1. Pick a market and get the token_id (YES or NO)
2. Use client.create_and_post_order():

from py_clob_client.order_builder.constants import BUY, SELL

# Example: Buy $1 of YES at 50% probability
order_args = {
    'token_id': 'TOKEN_ID_HERE',  # Get from market data
    'price': 0.50,                 # 50 cents = 50% probability
    'size': 1.0,                   # $1 worth
    'side': BUY,                   # BUY or SELL
    'fee_rate_bps': 0             # Fee in basis points
}

# This will sign and post the order
signed_order = client.create_and_post_order(order_args)
print(f"Order ID: {signed_order['orderID']}")

3. When the order fills, USDC.e is automatically pulled from your wallet
""")

print("\n" + "=" * 60)
print("✅ YOU'RE READY TO TRADE!")
print("=" * 60)
print("\nNext steps:")
print("1. Browse markets at: https://polymarket.com")
print("2. Use the Gamma API to get market data")
print("3. Place orders with client.create_and_post_order()")
print("4. Funds are pulled from your wallet only when orders fill")
