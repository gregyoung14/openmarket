#!/usr/bin/env python3
"""
Monitor and auto-sell DOWN contracts when price reaches target
"""
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType
from py_clob_client.order_builder.constants import SELL
from py_clob_client.exceptions import PolyApiException
import requests
import json
import time
from datetime import datetime, timezone

# Configuration
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"
MARKET_SLUG = "btc-updown-15m-1768159800"  # The market where we hold our position
TARGET_PRICE = 0.60  # Sell when DOWN price reaches $0.60
POSITION_SIZE = 2.0  # Amount of shares to sell
CHECK_INTERVAL = 0.05   # Check every 50 milliseconds
MARKET_REFRESH_INTERVAL = 0.05  # Refresh market data every 50 milliseconds

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

print("=" * 60)
print("POLYMARKET AUTO-SELL MONITOR")
print("=" * 60)
print(f"\nMarket: {MARKET_SLUG}")
print(f"Target: Sell {POSITION_SIZE} DOWN shares at ${TARGET_PRICE:.2f}")
print(f"Check interval: {CHECK_INTERVAL * 1000:.0f} milliseconds")
print(f"Market refresh: {MARKET_REFRESH_INTERVAL * 1000:.0f} milliseconds")

# Initialize client
print("\nInitializing client...")
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
try:
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    print("✅ Ready to trade")
except Exception as e:
    print(f"⚠️  Could not derive creds: {e}")

# Load our market info
print(f"\nLoading market: {MARKET_SLUG}...")
response = requests.get(f"https://gamma-api.polymarket.com/events?slug={MARKET_SLUG}")

if response.status_code != 200:
    print(f"❌ Failed to load market")
    exit(1)

events = response.json()
if not events or len(events) == 0:
    print(f"❌ Market not found")
    exit(1)

event = events[0]
market = event['markets'][0]
outcomes = json.loads(market['outcomes'])
token_ids = json.loads(market['clobTokenIds'])
prices = json.loads(market['outcomePrices'])

down_token = token_ids[1]  # DOWN is second token
down_price = float(prices[1])
neg_risk = market.get('negRisk', False)

print(f"✅ Market loaded")
print(f"   Title: {event.get('title')}")
print(f"   Current DOWN price: ${down_price:.4f}")
print(f"   DOWN token: {down_token[:20]}...")

def find_current_market():
    """Refresh market data for our specific market"""
    try:
        response = requests.get(f"https://gamma-api.polymarket.com/events?slug={MARKET_SLUG}")
        if response.status_code == 200:
            events = response.json()
            if events and len(events) > 0:
                event = events[0]
                market = event['markets'][0]
                prices = json.loads(market['outcomePrices'])
                
                return {
                    'slug': MARKET_SLUG,
                    'event': event,
                    'market': market,
                    'down_token': down_token,
                    'down_price': float(prices[1]),
                    'neg_risk': market.get('negRisk', False)
                }
    except Exception as e:
        print(f"Error refreshing market: {e}")
    
    return None

# Main monitoring loop
print("\n" + "=" * 60)
print("MONITORING STARTED")
print("=" * 60)

current_market = {
    'slug': MARKET_SLUG,
    'event': event,
    'market': market,
    'down_token': down_token,
    'down_price': down_price,
    'neg_risk': neg_risk
}
last_check_time = 0

try:
    while True:
        now = time.time()
        
        # Refresh market info every 50 milliseconds
        if (now - last_check_time) > MARKET_REFRESH_INTERVAL:
            refreshed = find_current_market()
            if refreshed:
                current_market = refreshed
            last_check_time = now
        
        # Check current price
        down_price = current_market['down_price']
        
        timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
        print(f"[{timestamp}] DOWN price: ${down_price:.4f} | Target: ${TARGET_PRICE:.2f}", end='')
        
        # Check if target reached
        if down_price >= TARGET_PRICE:
            print(f"\n\n{'=' * 60}")
            print(f"🎯 TARGET REACHED! DOWN at ${down_price:.4f}")
            print(f"{'=' * 60}")
            
            # Place SELL order
            print(f"\nSelling {POSITION_SIZE} DOWN shares at market...")
            
            # Calculate sell price (slightly below market for quick fill)
            sell_price = round(down_price * 0.98, 4)  # 2% below market
            
            try:
                order_args = OrderArgs(
                    token_id=current_market['down_token'],
                    price=sell_price,
                    size=POSITION_SIZE,
                    side=SELL
                )
                
                options = PartialCreateOrderOptions(neg_risk=current_market['neg_risk'])
                signed_order = client.create_order(order_args, options)
                
                print(f"Order created: SELL {POSITION_SIZE} @ ${sell_price:.4f}")
                
                # Post order with FOK (Fill or Kill) for immediate execution
                response = client.post_order(signed_order, OrderType.FOK)
                
                print(f"\n✅ ORDER EXECUTED!")
                print(f"=" * 60)
                print(f"Order ID: {response.get('orderID')}")
                print(f"Status: {response.get('status')}")
                print(f"Sold: {POSITION_SIZE} shares")
                print(f"Price: ${down_price:.4f}")
                print(f"\nDetails:")
                print(json.dumps(response, indent=2))
                
                if response.get('transactionsHashes'):
                    tx_hash = response['transactionsHashes'][0]
                    print(f"\nTransaction: https://polygonscan.com/tx/{tx_hash}")
                
                print(f"\n{'=' * 60}")
                print(f"✅ POSITION CLOSED - EXITING")
                print(f"{'=' * 60}")
                break
                
            except PolyApiException as e:
                print(f"\n❌ Failed to sell: {e}")
                # Continue monitoring
        else:
            gap = TARGET_PRICE - down_price
            print(f" | Gap: ${gap:.4f}", end='\r')
        
        time.sleep(CHECK_INTERVAL)

except KeyboardInterrupt:
    print(f"\n\n{'=' * 60}")
    print("⏹️  Monitoring stopped by user")
    print(f"{'=' * 60}")
except Exception as e:
    print(f"\n\n❌ Error: {e}")
    import traceback
    traceback.print_exc()
