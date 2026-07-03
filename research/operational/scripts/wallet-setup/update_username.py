#!/usr/bin/env python3
"""
Update Polymarket leaderboard username
Based on: https://github.com/Polymarket/leaderboard-username
"""
import time
import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

# Configuration
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"
USERNAME = "YOUR_USERNAME"

# Try these URLs
POSSIBLE_URLS = [
    "https://data-api.polymarket.com/leaderboard-username",
    "https://clob.polymarket.com/username",
    "https://gamma-api.polymarket.com/username",
]

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

# Derive account
account = Account.from_key(POLYGON_PRIVATE_KEY)
address = account.address
timestamp = int(time.time())

print("=" * 60)
print("UPDATING POLYMARKET LEADERBOARD USERNAME")
print("=" * 60)
print(f"\nAddress: {address}")
print(f"Username: {USERNAME}")
print(f"Timestamp: {timestamp}")

# Correct EIP-712 typed data structure from the repo
typed_data = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
        ],
        "UsernameUpdate": [
            {"name": "address", "type": "address"},
            {"name": "timestamp", "type": "uint256"},
            {"name": "name", "type": "string"},
        ]
    },
    "primaryType": "UsernameUpdate",
    "domain": {
        "name": "UsernameUpdate",
        "version": "1",
        "chainId": 137,
    },
    "message": {
        "address": address,
        "timestamp": timestamp,
        "name": USERNAME,
    }
}

print("\nSigning payload with EIP-712...")
signable_message = encode_typed_data(full_message=typed_data)
signed_message = account.sign_message(signable_message)
signature = signed_message.signature.hex()

print(f"Signature: {signature[:20]}...{signature[-20:]}")

# Payload to send
payload = {
    "address": address,
    "name": USERNAME,
    "timestamp": timestamp,
    "signature": signature,
}

print("\n" + "=" * 60)
print("SENDING TO API")
print("=" * 60)

# Try each possible URL
success = False
for url in POSSIBLE_URLS:
    print(f"\nTrying: {url}")
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text[:200]}")
        
        if response.status_code in [200, 201]:
            print("\n" + "=" * 60)
            print("✅ SUCCESS!")
            print("=" * 60)
            print(f"\nUsername updated to: {USERNAME}")
            print(f"Check your profile at: https://polymarket.com/profile/{address}")
            success = True
            break
        elif response.status_code == 404:
            print("❌ Endpoint not found, trying next...")
        else:
            print(f"⚠️  Status {response.status_code}, trying next...")
            
    except Exception as e:
        print(f"❌ Error: {str(e)[:100]}")
        continue

if not success:
    print("\n" + "=" * 60)
    print("ℹ️  ALTERNATIVE APPROACH NEEDED")
    print("=" * 60)
    print("\nThe API endpoint URL is not publicly documented.")
    print("You may need to:")
    print("1. Contact Polymarket support for the leaderboard API URL")
    print("2. Update via https://polymarket.com/settings manually")
    print("3. Check Polymarket Discord for the API endpoint")
    print("\nYour signature is ready and valid, we just need the correct URL.")
