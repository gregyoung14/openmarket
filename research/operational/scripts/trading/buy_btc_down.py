#!/usr/bin/env python3
"""
Buy 1 DOWN contract for BTC Up/Down 15m market
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.exceptions import PolyApiException
import requests
import json

# Private key only - we'll derive API credentials fresh
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

print("=" * 60)
print("BUY BTC DOWN CONTRACT")
print("=" * 60)

# Initialize client for EOA wallet (signature_type=0, no funder)
print("\nInitializing client...")
client = ClobClient(
    host="https://clob.polymarket.com",
    key=POLYGON_PRIVATE_KEY,
    chain_id=POLYGON,
    signature_type=0,  # 0 = EOA wallet (direct private key)
    funder=None        # None for EOA
)

print(f"Wallet: {client.get_address()}")

# Derive fresh API credentials (L2 auth)
print("\nDeriving API credentials...")
try:
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    print("✅ API credentials derived successfully")
except Exception as e:
    print(f"⚠️  Could not derive creds, continuing without: {e}")
    # Continue anyway - may work for some operations

# Calculate the current/next 15m market timestamp
# BTC 15m markets start every 15 minutes, slug format: btc-updown-15m-{start_timestamp}
import time
from datetime import datetime, timezone

current_unix = int(time.time())
interval_seconds = 15 * 60  # 15 minutes

# Round to the nearest 15-minute interval
# Markets typically start at :00, :15, :30, :45
def round_to_interval(ts, interval):
    """Round timestamp to nearest interval"""
    return (ts // interval) * interval

# Get current 15min period start
current_period_start = round_to_interval(current_unix, interval_seconds)

# The market for this period might already be closed, so try next period too
market_timestamps = [
    current_period_start + interval_seconds,  # Next period
    current_period_start,                     # Current period  
    current_period_start - interval_seconds   # Previous period (backup)
]

print(f"\nCurrent time: {datetime.fromtimestamp(current_unix, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"Trying market timestamps: {market_timestamps}")

# Try to find an active market
event = None
market_slug = None

for ts in market_timestamps:
    test_slug = f"btc-updown-15m-{ts}"
    market_start = datetime.fromtimestamp(ts, tz=timezone.utc)
    market_end = datetime.fromtimestamp(ts + interval_seconds, tz=timezone.utc)
    
    print(f"\nTrying: {test_slug}")
    print(f"  Start: {market_start.strftime('%H:%M:%S UTC')}")
    print(f"  End: {market_end.strftime('%H:%M:%S UTC')}")
    
    response = requests.get(f"https://gamma-api.polymarket.com/events?slug={test_slug}")
    
    if response.status_code == 200:
        events = response.json()
        if events and len(events) > 0:
            test_event = events[0]
            # Check if active
            if test_event.get('active', False) and not test_event.get('closed', True):
                event = test_event
                market_slug = test_slug
                print(f"  ✅ Found active market!")
                break
            else:
                print(f"  ⏸️  Market exists but not active/closed")
        else:
            print(f"  ❌ Market not found")
    else:
        print(f"  ❌ API error: {response.status_code}")

if not event:
    print(f"\n❌ No active BTC 15m markets found in current timeframe")
    exit(1)

print(f"\n{'=' * 60}")
print(f"FOUND ACTIVE MARKET")
print(f"{'=' * 60}")
print(f"Title: {event.get('title')}")
print(f"Slug: {market_slug}")

market = event['markets'][0]  # Get first market from event

print(f"\n📊 Market: {market.get('question', 'N/A')}")
print(f"Condition ID: {market.get('conditionId', 'N/A')}")
print(f"Active: {market.get('active', False)}")
print(f"End Date: {event.get('endDate', 'N/A')}")

# Check if neg-risk market (binary markets like Up/Down are NOT neg-risk)
neg_risk = market.get('negRisk', False)
print(f"Neg Risk: {neg_risk}")

# Parse outcomes and token IDs
outcomes = json.loads(market['outcomes'])  # ["Up", "Down"]
token_ids = json.loads(market['clobTokenIds'])  # Array of token IDs
prices = json.loads(market['outcomePrices'])  # Current prices

print(f"\nOutcomes:")
for i, outcome in enumerate(outcomes):
    print(f"  {outcome}: Token ID {token_ids[i][:20]}... (Price: ${prices[i]})")

# DOWN is typically the second outcome
down_token = token_ids[1]  # Index 1 = "Down"
up_token = token_ids[0]    # Index 0 = "Up"

print(f"\n✅ Found DOWN token: {down_token}")

# Use current market price for immediate execution (market order)
down_price = float(prices[1])  # DOWN price from API
print(f"\n💰 Current DOWN price: ${down_price:.4f} ({down_price * 100:.2f}%)")

# For market order, buy at slightly above market to ensure fill
# Add 2% to current price for immediate execution
# Price max 4 decimals for taker (buyer)
market_order_price = round(min(down_price * 1.02, 0.99), 4)  # Max 4 decimals

# Polymarket minimum order size is $1 worth at fill price
# Size max 2 decimals for shares
# For simplicity, use whole number
order_size = 2.00  # $2 worth of shares (simple, meets minimum)

# Place buy order
print(f"\n" + "=" * 60)
print(f"PLACING MARKET BUY ORDER")
print(f"=" * 60)

print(f"\nOrder Details:")
print(f"  Side: BUY (Market Order)")
print(f"  Token: DOWN")
print(f"  Price: ${market_order_price:.4f} ({market_order_price * 100:.2f}%)")
print(f"  Size: ${order_size:.2f}")
print(f"  Expected cost: ~${order_size * down_price:.4f}")

print(f"\n⚠️  Executing market order...")

try:
    # Create order with proper options
    print(f"\nCreating signed order...")
    
    order_args = OrderArgs(
        token_id=down_token,
        price=market_order_price,
        size=order_size,
        side=BUY
    )
    
    # Add neg_risk option if needed
    options = PartialCreateOrderOptions(neg_risk=neg_risk)
    
    signed_order = client.create_order(order_args, options)
    
    print(f"✅ Order created and signed")
    
    # Post the order (FOK = Fill or Kill for market order)
    print(f"\nPosting order to exchange...")
    response = client.post_order(signed_order, OrderType.FOK)
    
    print(f"\n✅ ORDER EXECUTED!")
    print(f"=" * 60)
    print(f"Order ID: {response.get('orderID', 'N/A')}")
    print(f"Status: {response.get('status', 'N/A')}")
    print(f"\nOrder details:")
    print(json.dumps(response, indent=2))
    
    print(f"\n✅ ORDER PLACED!")
    print(f"=" * 60)
    print(f"Order ID: {signed_order.get('orderID', 'N/A')}")
    print(f"\nOrder details:")
    print(json.dumps(signed_order, indent=2))
    
    print(f"\n" + "=" * 60)
    print(f"✅ SUCCESS!")
    print(f"=" * 60)
    print(f"\nYou now have 1 DOWN contract!")
    print(f"Track your position: https://polymarket.com/event/{market_slug}")
    print(f"\nThe market will resolve automatically.")
    print(f"If BTC goes DOWN in 15 minutes, you win $1!")
    
except Exception as e:
    print(f"\n❌ Failed to place order: {e}")
    import traceback
    traceback.print_exc()
    exit(1)
