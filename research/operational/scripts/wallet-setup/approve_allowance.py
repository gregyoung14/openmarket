#!/usr/bin/env python3
"""Quick script to approve unlimited USDC allowance"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from services.trading_service import TradingService
import config

# Initialize trading service with credentials from config
trading_service = TradingService(
    private_key=config.POLYGON_PRIVATE_KEY,
    api_key=config.POLYMARKET_API_KEY,
    api_secret=config.POLYMARKET_SECRET,
    api_passphrase=config.POLYMARKET_PASSPHRASE
)

print("🔄 Approving unlimited USDC allowance...")
print("This will allow the Polymarket exchange to spend your USDC.")
print()

result = trading_service.set_max_usdc_allowance()

if result.get("success"):
    print("✓ SUCCESS!")
    print(f"Transaction Hash: {result.get('tx_hash')}")
    print(f"New Allowance: ${result.get('new_allowance', 'MAX')}")
else:
    print("✗ FAILED")
    print(f"Error: {result.get('error')}")
    sys.exit(1)
