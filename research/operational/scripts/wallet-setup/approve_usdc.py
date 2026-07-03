#!/usr/bin/env python3
"""Direct USDC approval script using web3"""

from web3 import Web3
import config

# Polygon RPC
w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))

# USDC.e contract on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket CLOB exchange contract (spender)
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# ERC20 ABI for approve function
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }
]

print("🔐 Polygon USDC Allowance Approval")
print("=" * 50)
print(f"Network: Polygon (Chain ID: {w3.eth.chain_id})")
print(f"USDC Contract: {USDC_ADDRESS}")
print(f"Spender (Exchange): {EXCHANGE_ADDRESS}")
print()

# Get account from private key
account = w3.eth.account.from_key(config.POLYGON_PRIVATE_KEY)
print(f"Wallet: {account.address}")

# Get current allowance
usdc_contract = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)

# Maximum approval amount (2^256 - 1)
MAX_APPROVAL = 2**256 - 1

print(f"\n📝 Preparing transaction...")
print(f"Amount: UNLIMITED (2^256-1)")
print()

# Build transaction
txn = usdc_contract.functions.approve(
    EXCHANGE_ADDRESS,
    MAX_APPROVAL
).build_transaction({
    'from': account.address,
    'nonce': w3.eth.get_transaction_count(account.address),
    'gas': 100000,
    'gasPrice': w3.eth.gas_price
})

print(f"Gas Price: {w3.from_wei(txn['gasPrice'], 'gwei')} gwei")
print(f"Estimated Gas: {txn['gas']}")
print(f"Estimated Cost: {w3.from_wei(txn['gas'] * txn['gasPrice'], 'ether')} MATIC")
print()

# Sign and send
print("🚀 Sending transaction...")
signed_txn = account.sign_transaction(txn)
tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)

print(f"✓ Transaction submitted!")
print(f"TX Hash: {tx_hash.hex()}")
print()
print("⏳ Waiting for confirmation...")

# Wait for receipt
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

if receipt['status'] == 1:
    print("✅ SUCCESS!")
    print(f"Block: {receipt['blockNumber']}")
    print(f"Gas Used: {receipt['gasUsed']}")
    print()
    print("✓ USDC allowance set to UNLIMITED")
    print("✓ You can now place unlimited sell orders!")
else:
    print("❌ FAILED")
    print("Transaction reverted")
