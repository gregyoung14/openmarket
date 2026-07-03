#!/usr/bin/env python3
"""
Deposit USDC to Polymarket by transferring to your Polymarket proxy wallet
"""
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds
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
print("POLYMARKET DEPOSIT - TRANSFER METHOD")
print("=" * 60)
print(f"\nYour wallet: {my_address}")

# Connect to Polygon
rpc_url = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(rpc_url))

if not w3.is_connected():
    print("❌ Error: Could not connect to Polygon RPC")
    exit(1)

# Initialize CLOB client to get proxy address
print("\nInitializing ClobClient to get your Polymarket proxy address...")
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

# Get the proxy wallet address (this is where you need to send USDC!)
proxy_address = client.get_address()
print(f"Your Polymarket proxy: {proxy_address}")

# USDC.e contract
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

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
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }
]

usdc_contract = w3.eth.contract(
    address=Web3.to_checksum_address(USDC_ADDRESS),
    abi=erc20_abi
)

# Check balances
print("\n" + "=" * 60)
print("CHECKING BALANCES")
print("=" * 60)

usdc_wei = usdc_contract.functions.balanceOf(my_address).call()
decimals = usdc_contract.functions.decimals().call()
usdc_balance = usdc_wei / (10 ** decimals)
print(f"Your wallet USDC: {usdc_balance} USDC")

proxy_usdc_wei = usdc_contract.functions.balanceOf(proxy_address).call()
proxy_usdc_balance = proxy_usdc_wei / (10 ** decimals)
print(f"Proxy wallet USDC: {proxy_usdc_balance} USDC")

if usdc_balance == 0:
    print("\n❌ No USDC to deposit!")
    exit(1)

# Transfer USDC to proxy
print("\n" + "=" * 60)
print("TRANSFERRING USDC TO POLYMARKET PROXY")
print("=" * 60)
print(f"\nTransferring {usdc_balance} USDC")
print(f"From: {my_address}")
print(f"To: {proxy_address} (Polymarket Proxy)")

# Build transfer transaction
transfer_txn = usdc_contract.functions.transfer(
    Web3.to_checksum_address(proxy_address),
    usdc_wei
).build_transaction({
    'from': my_address,
    'nonce': w3.eth.get_transaction_count(my_address),
    'gas': 100000,
    'maxFeePerGas': w3.eth.gas_price,
    'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
    'chainId': 137
})

# Sign and send
signed_txn = account.sign_transaction(transfer_txn)
tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)

print(f"\nTransaction: {tx_hash.hex()}")
print("Waiting for confirmation...")

receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

if receipt['status'] == 1:
    print("✅ Transfer successful!")
    print(f"\nView on PolygonScan: https://polygonscan.com/tx/{tx_hash.hex()}")
    
    # Now sync with Polymarket API
    print("\n" + "=" * 60)
    print("SYNCING WITH POLYMARKET")
    print("=" * 60)
    print("\nWaiting 10 seconds for blockchain confirmation...")
    time.sleep(10)
    
    print("Calling update_balance_allowance to sync with Polymarket...")
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=2
    )
    
    result = client.update_balance_allowance(params=params)
    print(f"Sync result: {result}")
    
    # Check final balance
    balance_info = client.get_balance_allowance(params=params)
    final_balance = float(balance_info.get('balance', '0')) / 10**6
    
    print("\n" + "=" * 60)
    print("✅ SUCCESS!")
    print("=" * 60)
    print(f"Polymarket balance: {final_balance} USDC")
    print("\nYou can now trade on Polymarket! 🚀")
    print("=" * 60)
else:
    print("❌ Transfer failed!")
    exit(1)
