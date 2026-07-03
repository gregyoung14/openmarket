#!/usr/bin/env python3
"""
Approve CTF Exchange and sync balance with Polymarket
"""
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
import time

# Credentials
POLYMARKET_API_KEY = "REPLACE_WITH_POLYMARKET_API_KEY"
POLYMARKET_SECRET = "REPLACE_WITH_POLYMARKET_SECRET"
POLYMARKET_PASSPHRASE = "REPLACE_WITH_POLYMARKET_PASSPHRASE"
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

# Derive account
account = Account.from_key(POLYGON_PRIVATE_KEY)
my_address = account.address

print("=" * 60)
print("POLYMARKET - APPROVE & SYNC BALANCE")
print("=" * 60)

# Connect to Polygon
rpc_url = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(rpc_url))

# Contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

erc20_abi = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]

usdc_contract = w3.eth.contract(
    address=Web3.to_checksum_address(USDC_ADDRESS),
    abi=erc20_abi
)

# Check USDC balance
usdc_wei = usdc_contract.functions.balanceOf(my_address).call()
decimals = usdc_contract.functions.decimals().call()
usdc_balance = usdc_wei / (10 ** decimals)

print(f"\nYour wallet: {my_address}")
print(f"USDC balance: {usdc_balance} USDC")

# Check current allowance
current_allowance = usdc_contract.functions.allowance(
    my_address,
    Web3.to_checksum_address(CTF_EXCHANGE)
).call()
current_allowance_usdc = current_allowance / (10 ** decimals)

print(f"Current CTF Exchange allowance: {current_allowance_usdc} USDC")

# Approve unlimited (max uint256)
if current_allowance < usdc_wei:
    print("\n" + "=" * 60)
    print("STEP 1: APPROVING CTF EXCHANGE")
    print("=" * 60)
    
    # Use max uint256 for unlimited approval
    max_uint256 = 2**256 - 1
    
    approve_txn = usdc_contract.functions.approve(
        Web3.to_checksum_address(CTF_EXCHANGE),
        max_uint256
    ).build_transaction({
        'from': my_address,
        'nonce': w3.eth.get_transaction_count(my_address),
        'gas': 100000,
        'maxFeePerGas': w3.eth.gas_price,
        'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
        'chainId': 137
    })
    
    signed_approve = account.sign_transaction(approve_txn)
    approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
    
    print(f"Approval TX: {approve_hash.hex()}")
    print("Waiting for confirmation...")
    
    approve_receipt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
    
    if approve_receipt['status'] == 1:
        print("✅ Approval successful!")
        print(f"View on PolygonScan: https://polygonscan.com/tx/{approve_hash.hex()}")
    else:
        print("❌ Approval failed!")
        exit(1)
else:
    print("\n✅ Already approved!")

# Now sync with Polymarket
print("\n" + "=" * 60)
print("STEP 2: SYNCING WITH POLYMARKET API")
print("=" * 60)

print("\nInitializing ClobClient...")
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

params = BalanceAllowanceParams(
    asset_type=AssetType.COLLATERAL,
    signature_type=2
)

print("Updating balance allowance...")
result = client.update_balance_allowance(params=params)
print(f"Update result: {result}")

time.sleep(5)

print("\nChecking Polymarket balance...")
balance_info = client.get_balance_allowance(params=params)
polymarket_balance = float(balance_info.get('balance', '0')) / 10**6
allowance = float(balance_info.get('allowance', '0')) / 10**6

print("\n" + "=" * 60)
print("FINAL BALANCES")
print("=" * 60)
print(f"Wallet USDC: {usdc_balance} USDC")
print(f"Polymarket balance: {polymarket_balance} USDC")
print(f"Polymarket allowance: {allowance} USDC")

if polymarket_balance > 0:
    print("\n" + "=" * 60)
    print("✅ SUCCESS! You can now trade on Polymarket! 🚀")
    print("=" * 60)
else:
    print("\n⚠️  Balance still 0. This might take a few minutes to sync.")
    print("Try running check_polymarket_balance.py in a few minutes.")
