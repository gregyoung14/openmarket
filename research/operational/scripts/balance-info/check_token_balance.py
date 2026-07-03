#!/usr/bin/env python3
"""
Check token holdings for the DOWN token directly
"""
from web3 import Web3
from eth_account import Account
import requests
import json

POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"
MARKET_SLUG = "btc-updown-15m-1768159800"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

account = Account.from_key(POLYGON_PRIVATE_KEY)
my_address = account.address

# Get market data
response = requests.get(f"https://gamma-api.polymarket.com/events?slug={MARKET_SLUG}")
event = response.json()[0]
market = event['markets'][0]

token_ids = json.loads(market['clobTokenIds'])
down_token = token_ids[1]

print(f"Wallet: {my_address}")
print(f"DOWN Token: {down_token}")

# Connect to Polygon
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

# ERC1155 ABI for balanceOf
erc1155_abi = [{
    "constant": True,
    "inputs": [
        {"name": "account", "type": "address"},
        {"name": "id", "type": "uint256"}
    ],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function"
}]

# CTF (Conditional Token Framework) contract
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
ctf_contract = w3.eth.contract(
    address=Web3.to_checksum_address(CTF_ADDRESS),
    abi=erc1155_abi
)

print(f"\nChecking balance on-chain...")
balance = ctf_contract.functions.balanceOf(
    Web3.to_checksum_address(my_address),
    int(down_token)
).call()

print(f"\n{'=' * 60}")
print(f"DOWN TOKEN BALANCE")
print(f"{'=' * 60}")
print(f"Balance: {balance} contracts")
print(f"Value: ${balance / 10**6:.6f}")

if balance > 0:
    print(f"\n✅ You have {balance} DOWN contracts to sell!")
else:
    print(f"\n❌ No DOWN contracts found")
