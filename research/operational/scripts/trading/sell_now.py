#!/usr/bin/env python3
"""
Sell DOWN contracts immediately at market price
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType
from py_clob_client.order_builder.constants import SELL
from py_clob_client.exceptions import PolyApiException
import requests
import json

POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"
MARKET_SLUG = "btc-updown-15m-1768159800"
POSITION_SIZE = 1.97  # Actual position size from on-chain balance

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

print("=" * 60)
print("SELL DOWN CONTRACTS NOW")
print("=" * 60)

# Initialize client
client = ClobClient(
    host="https://clob.polymarket.com",
    key=POLYGON_PRIVATE_KEY,
    chain_id=POLYGON,
    signature_type=0,
    funder=None
)

print(f"Wallet: {client.get_address()}")

# Derive API credentials
print("Deriving API credentials...")
api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)
print("✅ Ready")

# Get market data
print(f"\nLoading market: {MARKET_SLUG}...")
response = requests.get(f"https://gamma-api.polymarket.com/events?slug={MARKET_SLUG}")
event = response.json()[0]
market = event['markets'][0]

token_ids = json.loads(market['clobTokenIds'])
prices = json.loads(market['outcomePrices'])

down_token = token_ids[1]
down_price = float(prices[1])
neg_risk = market.get('negRisk', False)

print(f"Current DOWN price: ${down_price:.4f}")

# Calculate sell price (2% below market for immediate fill)
sell_price = round(down_price * 0.98, 4)

print(f"\n{'=' * 60}")
print(f"EXECUTING SELL ORDER")
print(f"{'=' * 60}")
print(f"Selling {POSITION_SIZE} DOWN @ ${sell_price:.4f}")

try:
    order_args = OrderArgs(
        token_id=down_token,
        price=sell_price,
        size=POSITION_SIZE,
        side=SELL
    )
    
    options = PartialCreateOrderOptions(neg_risk=neg_risk)
    signed_order = client.create_order(order_args, options)
    
    # Execute with FOK (Fill or Kill)
    response = client.post_order(signed_order, OrderType.FOK)
    
    print(f"\n✅ ORDER EXECUTED!")
    print(f"{'=' * 60}")
    print(f"Order ID: {response.get('orderID')}")
    print(f"Status: {response.get('status')}")
    print(f"\nDetails:")
    print(json.dumps(response, indent=2))
    
    if response.get('transactionsHashes'):
        tx_hash = response['transactionsHashes'][0]
        print(f"\nTransaction: https://polygonscan.com/tx/{tx_hash}")
    
    print(f"\n{'=' * 60}")
    print(f"✅ POSITION CLOSED!")
    print(f"{'=' * 60}")
    
except PolyApiException as e:
    print(f"\n❌ Failed: {e}")
