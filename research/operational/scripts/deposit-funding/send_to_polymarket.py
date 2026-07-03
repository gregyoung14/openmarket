#!/usr/bin/env python3
"""
Send USDC to Polymarket deposit address
"""
from web3 import Web3
from eth_account import Account

# Configuration
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"
POLYMARKET_DEPOSIT_ADDRESS = "POLYMARKET_DEPOSIT_ADDRESS"
USDC_CONTRACT_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC on Polygon

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

# Derive account
account = Account.from_key(POLYGON_PRIVATE_KEY)
my_address = account.address

# Connect to Polygon
rpc_url = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(rpc_url))

if not w3.is_connected():
    print("❌ Error: Could not connect to Polygon RPC")
    exit(1)

print("=" * 60)
print("SENDING USDC TO POLYMARKET")
print("=" * 60)
print(f"\nFrom: {my_address}")
print(f"To: {POLYMARKET_DEPOSIT_ADDRESS}")
print(f"Connected to Polygon (Chain ID: {w3.eth.chain_id})")

# USDC ABI
usdc_abi = [
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

# Initialize USDC contract
usdc_contract = w3.eth.contract(
    address=Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),
    abi=usdc_abi
)

# Check balance
usdc_wei = usdc_contract.functions.balanceOf(my_address).call()
decimals = usdc_contract.functions.decimals().call()
usdc_balance = usdc_wei / (10 ** decimals)

print(f"\nCurrent USDC Balance: {usdc_balance} USDC")

if usdc_balance == 0:
    print("❌ No USDC to send!")
    exit(1)

# Send all USDC
amount_to_send_wei = usdc_wei
amount_to_send = usdc_balance

print(f"Sending: {amount_to_send} USDC")

# Check MATIC for gas
matic_wei = w3.eth.get_balance(my_address)
matic_balance = matic_wei / (10 ** 18)
print(f"MATIC for gas: {matic_balance} MATIC")

if matic_balance < 0.001:
    print("⚠️  Warning: Very low MATIC balance for gas")

# Build transaction
print("\nBuilding transaction...")
nonce = w3.eth.get_transaction_count(my_address)

tx = usdc_contract.functions.transfer(
    Web3.to_checksum_address(POLYMARKET_DEPOSIT_ADDRESS),
    amount_to_send_wei
).build_transaction({
    'from': my_address,
    'nonce': nonce,
    'gas': 100000,
    'maxFeePerGas': w3.eth.gas_price,
    'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
    'chainId': 137
})

print("Signing transaction...")
signed_tx = account.sign_transaction(tx)

print("Sending transaction...")
tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

print(f"\n✅ Transaction sent!")
print(f"TX Hash: {tx_hash.hex()}")
print(f"View on PolygonScan: https://polygonscan.com/tx/{tx_hash.hex()}")

print("\nWaiting for confirmation...")
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

if receipt['status'] == 1:
    print("\n" + "=" * 60)
    print("✅ SUCCESS!")
    print("=" * 60)
    print(f"\n{amount_to_send} USDC sent to Polymarket!")
    print(f"Transaction confirmed in block: {receipt['blockNumber']}")
    print(f"\nYour USDC should appear in Polymarket within 1-5 minutes.")
    print("Check your balance at: https://polymarket.com/cash")
    print("=" * 60)
else:
    print("\n❌ Transaction failed!")
    print(f"Receipt: {receipt}")
