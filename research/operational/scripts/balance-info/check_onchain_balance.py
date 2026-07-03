#!/usr/bin/env python3
"""
Check on-chain wallet balance on Polygon
"""
import sys
from pathlib import Path

from web3 import Web3

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.wallet_env import connect_polygon, get_account  # noqa: E402

# Derive address
account = get_account()
my_address = account.address
print(f"Wallet Address: {my_address}\n")

# Connect to Polygon using public RPC
w3 = connect_polygon()

if not w3.is_connected():
    print("❌ Error: Could not connect to Polygon RPC")
    exit(1)

print(f"✅ Connected to Polygon (Chain ID: {w3.eth.chain_id})\n")

# USDC on Polygon (native USDC, not bridged)
# There are two USDC tokens on Polygon:
# 1. USDC.e (bridged from Ethereum): 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
# 2. Native USDC: 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359

usdc_addresses = {
    "USDC.e (Bridged)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC (Native)": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
}

# Minimal ERC20 ABI for balanceOf and decimals
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
    }
]

print("=" * 60)
print("USDC BALANCES")
print("=" * 60)

for name, address in usdc_addresses.items():
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=erc20_abi)
        balance_wei = contract.functions.balanceOf(my_address).call()
        decimals = contract.functions.decimals().call()
        balance = balance_wei / (10 ** decimals)
        print(f"{name}: {balance} USDC")
    except Exception as e:
        print(f"{name}: Error - {e}")

print("\n" + "=" * 60)
print("POL/MATIC BALANCE (for gas)")
print("=" * 60)

try:
    matic_wei = w3.eth.get_balance(my_address)
    matic_balance = matic_wei / (10 ** 18)
    print(f"POL/MATIC: {matic_balance}")
except Exception as e:
    print(f"Error fetching MATIC balance: {e}")

print("\n" + "=" * 60)
