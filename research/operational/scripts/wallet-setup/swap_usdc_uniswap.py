#!/usr/bin/env python3
"""
Uniswap V3 USDC Swap Script for Polygon

This script allows swapping between native USDC and USDC.e (bridged USDC) on Polygon
using Uniswap V3. It's designed for wallet management in the Polymarket trading system.

Usage Examples:
  # Swap all native USDC to USDC.e (default behavior)
  python3 swap_usdc_uniswap.py native-to-bridged

  # Swap all USDC.e to native USDC
  python3 swap_usdc_uniswap.py bridged-to-native

  # Swap specific amount with custom slippage
  python3 swap_usdc_uniswap.py native-to-bridged --amount 50 --slippage 1.0

  # Dry run to see what would happen
  python3 swap_usdc_uniswap.py native-to-bridged --dry-run

Addresses:
  Native USDC: 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
  USDC.e: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
  Uniswap V3 Router: 0xE592427A0AEce92De3Edee1F18E0157C05861564
"""

import os
import time
import argparse
from web3 import Web3

# Polygon RPC endpoint. Prefer POLYGON_RPC_URL; fall back to Alchemy only when
# ALCHEMY_API_KEY is supplied, then to a public RPC for dry-run style checks.
RPC_URL = os.environ.get("POLYGON_RPC_URL") or (
    f"https://polygon-mainnet.g.alchemy.com/v2/{os.environ['ALCHEMY_API_KEY']}"
    if os.environ.get("ALCHEMY_API_KEY")
    else "https://polygon-rpc.com"
)

# Token addresses
NATIVE_USDC = '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359'
BRIDGED_USDC = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'

# Uniswap V3 SwapRouter address
UNISWAP_ROUTER = '0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45'

# ERC20 ABI (minimal for approve and balanceOf)
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
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]

# Uniswap V3 SwapRouter ABI (minimal for exactInputSingle)
ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"}
                ],
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

def load_private_key():
    try:
        with open('/path/to/wallet-gen/.env.wallet', 'r') as f:
            content = f.read().strip()
            if content.startswith('PRIVATE_KEY='):
                return content.split('=', 1)[1]
            else:
                return content
    except FileNotFoundError:
        raise FileNotFoundError("Private key file '/path/to/wallet-gen/.env.wallet' not found.")

def approve_token(w3, account, private_key, token_address, spender, amount):
    contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    nonce = w3.eth.get_transaction_count(account.address)
    tx = contract.functions.approve(spender, amount).build_transaction({
        'from': account.address,
        'nonce': nonce,
        'gas': 100000,
        'gasPrice': w3.eth.gas_price
    })
    signed_tx = w3.eth.account.sign_transaction(tx, private_key)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt['status'] != 1:
                raise Exception("Approval transaction failed")
            print(f"Approved {amount} for {spender}")
            return
        except Exception as e:
            if "rate limit" in str(e).lower() and attempt < max_retries - 1:
                print(f"Rate limited, retrying in 15 seconds... (attempt {attempt + 1}/{max_retries})")
                time.sleep(15)
            else:
                raise e

def get_balance(w3, account, token_address):
    contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    return contract.functions.balanceOf(account.address).call()

def swap(w3, account, private_key, token_in, token_out, amount_in, amount_out_min):
    router = w3.eth.contract(address=UNISWAP_ROUTER, abi=ROUTER_ABI)
    
    # Try different fee tiers in order of preference for stablecoins
    fee_tiers = [500, 3000, 100, 10000]  # 0.05%, 0.3%, 0.01%, 1%
    
    for fee in fee_tiers:
        print(f"Trying fee tier: {fee/10000}%")
        
        params = {
            'tokenIn': token_in,
            'tokenOut': token_out,
            'fee': fee,
            'recipient': account.address,
            'amountIn': amount_in,
            'amountOutMinimum': amount_out_min,
            'sqrtPriceLimitX96': 0
        }
        
        try:
            # Test gas estimation first
            gas_estimate = router.functions.exactInputSingle(params).estimate_gas({
                'from': account.address
            })
            print(f"Gas estimate successful: {gas_estimate}")
            
            # If estimation succeeds, proceed with transaction
            nonce = w3.eth.get_transaction_count(account.address)
            tx = router.functions.exactInputSingle(params).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gas': int(gas_estimate * 1.2),  # Add 20% buffer
                'gasPrice': w3.eth.gas_price
            })
            signed_tx = w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt['status'] != 1:
                print(f"Transaction failed with status: {receipt['status']}")
                continue  # Try next fee tier
            print(f"Swap successful: {receipt['transactionHash'].hex()}")
            return
            
        except Exception as e:
            print(f"Fee tier {fee/10000}% failed: {e}")
            continue
    
    # If all fee tiers failed
    raise Exception("All fee tiers failed - no liquidity pool found for this token pair")

def main():
    parser = argparse.ArgumentParser(description='Swap USDC tokens on Polygon using Uniswap V3')
    parser.add_argument('direction', choices=['native-to-bridged', 'bridged-to-native'], help='Swap direction')
    parser.add_argument('--amount', type=float, help='Amount of USDC to swap (in USDC units). If not specified, uses maximum available balance.')
    parser.add_argument('--slippage', type=float, default=0.5, help='Slippage tolerance in percent (default: 0.5)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without executing the swap')
    args = parser.parse_args()

    # Initialize Web3
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise Exception("Failed to connect to Polygon RPC")

    # Load private key and account
    private_key = load_private_key()
    account = w3.eth.account.from_key(private_key)

    # Determine tokens
    if args.direction == 'native-to-bridged':
        token_in = NATIVE_USDC
        token_out = BRIDGED_USDC
    else:
        token_in = BRIDGED_USDC
        token_out = NATIVE_USDC

    # Get balance and determine amount
    balance = get_balance(w3, account, token_in)
    if args.amount is None:
        amount_in = balance
        print(f"Using maximum available balance: {balance / 10**6} USDC")
    else:
        amount_in = int(args.amount * 10**6)
        if balance < amount_in:
            raise ValueError(f"Insufficient balance: {balance / 10**6} USDC available, {args.amount} required")

    if amount_in == 0:
        print("No balance to swap")
        return

    amount_out_min = int(amount_in * (1 - args.slippage / 100))

    print(f"Swap details:")
    print(f"  Direction: {args.direction}")
    print(f"  From: {token_in}")
    print(f"  To: {token_out}")
    print(f"  Amount: {amount_in / 10**6} USDC")
    print(f"  Min output: {amount_out_min / 10**6} USDC (with {args.slippage}% slippage)")
    print(f"  Wallet: {account.address}")

    if args.dry_run:
        print("\n🔍 DRY RUN - No transactions executed")
        return

    # Approve token
    print("Preparing approval transaction...")
    time.sleep(2)  # Brief pause to avoid rate limiting
    approve_token(w3, account, private_key, token_in, UNISWAP_ROUTER, amount_in)

    # Perform swap
    print("Preparing swap transaction...")
    time.sleep(2)  # Brief pause to avoid rate limiting
    swap(w3, account, private_key, token_in, token_out, amount_in, amount_out_min)

if __name__ == '__main__':
    main()
