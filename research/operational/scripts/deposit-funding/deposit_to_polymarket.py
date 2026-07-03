#!/usr/bin/env python3
"""
Deposit USDC into Polymarket
"""
from web3 import Web3
from eth_account import Account
import time

# Your private key
POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

# Derive account
account = Account.from_key(POLYGON_PRIVATE_KEY)
my_address = account.address
print(f"Wallet Address: {my_address}\n")

# Connect to Polygon
rpc_url = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(rpc_url))

if not w3.is_connected():
    print("❌ Error: Could not connect to Polygon RPC")
    exit(1)

print(f"✅ Connected to Polygon (Chain ID: {w3.eth.chain_id})\n")

# Contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (Bridged) on Polygon - Polymarket uses this!
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polymarket's CTF Exchange

# ERC20 ABI
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

# CTF Exchange ABI (deposit function)
ctf_exchange_abi = [
    {
        "inputs": [
            {"name": "amount", "type": "uint256"}
        ],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# Initialize contracts
usdc_contract = w3.eth.contract(
    address=Web3.to_checksum_address(USDC_ADDRESS),
    abi=erc20_abi
)

ctf_exchange_contract = w3.eth.contract(
    address=Web3.to_checksum_address(CTF_EXCHANGE),
    abi=ctf_exchange_abi
)

# Check balances
print("=" * 60)
print("CHECKING BALANCES")
print("=" * 60)

matic_wei = w3.eth.get_balance(my_address)
matic_balance = matic_wei / (10 ** 18)
print(f"MATIC: {matic_balance} MATIC")

usdc_wei = usdc_contract.functions.balanceOf(my_address).call()
decimals = usdc_contract.functions.decimals().call()
usdc_balance = usdc_wei / (10 ** decimals)
print(f"USDC: {usdc_balance} USDC\n")

if matic_balance < 0.01:
    print("⚠️  WARNING: You have very low MATIC balance!")
    print("You need MATIC to pay for gas fees to deposit USDC.")
    print("\nTo get MATIC:")
    print("1. Use a faucet: https://faucet.polygon.technology/")
    print("2. Or send ~0.1 MATIC to your address from an exchange")
    print(f"\nYour address: {my_address}")
    print("\nOnce you have MATIC, run this script again.")
    exit(1)

if usdc_balance == 0:
    print("❌ No USDC to deposit!")
    exit(1)

# Check current allowance
current_allowance = usdc_contract.functions.allowance(
    my_address,
    Web3.to_checksum_address(CTF_EXCHANGE)
).call()

print("=" * 60)
print("DEPOSIT PROCESS")
print("=" * 60)

# Amount to deposit (all USDC)
deposit_amount_wei = usdc_wei
deposit_amount = usdc_balance

print(f"\nDepositing: {deposit_amount} USDC")
print(f"To: Polymarket CTF Exchange ({CTF_EXCHANGE})\n")

# Step 1: Approve if needed
if current_allowance < deposit_amount_wei:
    print("Step 1/2: Approving USDC spend...")
    
    # Build approve transaction
    approve_txn = usdc_contract.functions.approve(
        Web3.to_checksum_address(CTF_EXCHANGE),
        deposit_amount_wei
    ).build_transaction({
        'from': my_address,
        'nonce': w3.eth.get_transaction_count(my_address),
        'gas': 100000,
        'maxFeePerGas': w3.eth.gas_price,
        'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
        'chainId': 137
    })
    
    # Sign and send
    signed_approve = account.sign_transaction(approve_txn)
    approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
    
    print(f"Approval TX: {approve_hash.hex()}")
    print("Waiting for confirmation...")
    
    approve_receipt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
    
    if approve_receipt['status'] == 1:
        print("✅ Approval successful!\n")
    else:
        print("❌ Approval failed!")
        exit(1)
else:
    print("Step 1/2: Already approved ✅\n")

# Step 2: Deposit
print("Step 2/2: Depositing USDC to Polymarket...")

deposit_txn = ctf_exchange_contract.functions.deposit(
    deposit_amount_wei
).build_transaction({
    'from': my_address,
    'nonce': w3.eth.get_transaction_count(my_address),
    'gas': 200000,
    'maxFeePerGas': w3.eth.gas_price,
    'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
    'chainId': 137
})

# Sign and send
signed_deposit = account.sign_transaction(deposit_txn)
deposit_hash = w3.eth.send_raw_transaction(signed_deposit.raw_transaction)

print(f"Deposit TX: {deposit_hash.hex()}")
print("Waiting for confirmation...")

deposit_receipt = w3.eth.wait_for_transaction_receipt(deposit_hash, timeout=120)

if deposit_receipt['status'] == 1:
    print("✅ Deposit successful!")
    print(f"\nView on PolygonScan: https://polygonscan.com/tx/{deposit_hash.hex()}")
    print("\n" + "=" * 60)
    print("SUCCESS! Your USDC is now in Polymarket.")
    print("You can now trade on Polymarket!")
    print("=" * 60)
else:
    print("❌ Deposit failed!")
    exit(1)
