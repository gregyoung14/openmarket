#!/usr/bin/env python3
"""
Approve ERC1155 tokens for CTF Exchange and sell
"""
from web3 import Web3
from eth_account import Account
import requests
import json
import time

POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"
MARKET_SLUG = "btc-updown-15m-1768159800"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

account = Account.from_key(POLYGON_PRIVATE_KEY)
my_address = account.address

# Connect to Polygon
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

# Contracts
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESSES = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # Main binary
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # Neg-risk
    "0xd91E80cF2e7be2e162c6513ceD06f1dD0dA35296"   # Adapter
]

print("=" * 60)
print("APPROVE AND SELL DOWN CONTRACTS")
print("=" * 60)

# ERC1155 ABI
erc1155_abi = [{
    "constant": True,
    "inputs": [
        {"name": "account", "type": "address"},
        {"name": "id", "type": "uint256"}
    ],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function"
}, {
    "constant": True,
    "inputs": [
        {"name": "account", "type": "address"},
        {"name": "operator", "type": "address"}
    ],
    "name": "isApprovedForAll",
    "outputs": [{"name": "", "type": "bool"}],
    "type": "function"
}, {
    "constant": False,
    "inputs": [
        {"name": "operator", "type": "address"},
        {"name": "approved", "type": "bool"}
    ],
    "name": "setApprovalForAll",
    "outputs": [],
    "type": "function"
}]

ctf_contract = w3.eth.contract(
    address=Web3.to_checksum_address(CTF_ADDRESS),
    abi=erc1155_abi
)

# Check approvals for all exchanges
for exchange_addr in EXCHANGE_ADDRESSES:
    is_approved = ctf_contract.functions.isApprovedForAll(
        Web3.to_checksum_address(my_address),
        Web3.to_checksum_address(exchange_addr)
    ).call()
    print(f"CTF approval for {exchange_addr}: {is_approved}")
    
    if not is_approved:
        print(f"\nApproving {exchange_addr} to spend tokens...")
        
        approval_txn = ctf_contract.functions.setApprovalForAll(
            Web3.to_checksum_address(exchange_addr),
            True
        ).build_transaction({
            'from': my_address,
            'nonce': w3.eth.get_transaction_count(my_address),
            'gas': 100000,
            'maxFeePerGas': w3.eth.gas_price,
            'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
            'chainId': 137
        })
        
        signed_txn = account.sign_transaction(approval_txn)
        tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        
        print(f"Approval TX: {tx_hash.hex()}")
        print("Waiting for confirmation...")
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt['status'] == 1:
            print(f"✅ Approval successful for {exchange_addr}!")
        else:
            print(f"❌ Approval failed for {exchange_addr}!")
            exit(1)
        
        # Wait a bit between approvals
        time.sleep(2)
    else:
        print(f"✅ Already approved for {exchange_addr}")

print("\n✅ All approvals complete!")

# Now sell using CLOB client
print("\n" + "=" * 60)
print("SELLING TOKENS")
print("=" * 60)

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType
from py_clob_client.order_builder.constants import SELL

client = ClobClient(
    host="https://clob.polymarket.com",
    key=POLYGON_PRIVATE_KEY,
    chain_id=POLYGON,
    signature_type=0,
    funder=None
)

api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)

# Get market data
response = requests.get(f"https://gamma-api.polymarket.com/events?slug={MARKET_SLUG}")
event = response.json()[0]
market = event['markets'][0]

token_ids = json.loads(market['clobTokenIds'])
prices = json.loads(market['outcomePrices'])
down_token = token_ids[1]
down_price = float(prices[1])

print(f"Current DOWN price: ${down_price:.4f}")

# Use exact position size
balance = ctf_contract.functions.balanceOf(
    Web3.to_checksum_address(my_address),
    int(down_token)
).call()

position_size = balance / 10**6  # Convert to contracts
sell_price = round(down_price * 0.95, 4)  # More aggressive discount for market sell

print(f"Selling {position_size} DOWN @ ${sell_price:.4f} (current: ${down_price:.4f})")

try:
    order_args = OrderArgs(
        token_id=down_token,
        price=sell_price,
        size=position_size,
        side=SELL
    )
    
    options = PartialCreateOrderOptions(neg_risk=False)
    signed_order = client.create_order(order_args, options)
    response = client.post_order(signed_order, OrderType.FOK)
    
    print(f"\n✅ SOLD!")
    print(json.dumps(response, indent=2))
    
    if response.get('transactionsHashes'):
        print(f"\nTX: https://polygonscan.com/tx/{response['transactionsHashes'][0]}")
        
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
