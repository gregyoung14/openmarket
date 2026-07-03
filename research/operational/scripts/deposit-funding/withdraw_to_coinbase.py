#!/usr/bin/env python3
"""Withdraw a Polymarket Polygon wallet back to a CEX deposit address.

Flow:
  1. Swap Polygon USDC.e (bridged) to native Polygon USDC if needed.
  2. Send native Polygon USDC to the supplied deposit address.

Default mode is a dry-run. Pass --execute to broadcast transactions.
"""

import argparse
import time
import sys
from pathlib import Path as FsPath

from web3 import Web3

sys.path.append(str(FsPath(__file__).resolve().parents[1]))
from common.wallet_env import (  # noqa: E402
    BRIDGED_USDCE,
    ERC20_ABI,
    NATIVE_USDC,
    UNISWAP_QUOTER,
    UNISWAP_ROUTER,
    USDC_DECIMALS,
    connect_polygon,
    format_token,
    get_account,
    get_private_key,
)

MAX_SLIPPAGE_BPS = 50

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
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

QUOTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def send_and_wait(w3: Web3, account, private_key: str, tx) -> str:
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = w3.to_hex(tx_hash)
    print(f"Broadcast: {tx_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction failed: {tx_hex}")
    print(f"Confirmed in block {receipt['blockNumber']}")
    return tx_hex


def build_tx_base(w3: Web3, sender: str, nonce: int) -> dict:
    gas_price = w3.eth.gas_price
    return {
        "from": sender,
        "nonce": nonce,
        "gasPrice": gas_price,
        "chainId": 137,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("deposit_address", help="Native Polygon USDC deposit address")
    parser.add_argument("--execute", action="store_true", help="Broadcast transactions")
    args = parser.parse_args()

    private_key = get_private_key()
    account = get_account()
    recipient = Web3.to_checksum_address(args.deposit_address)
    w3 = connect_polygon()

    bridged = w3.eth.contract(address=BRIDGED_USDCE, abi=ERC20_ABI)
    native = w3.eth.contract(address=NATIVE_USDC, abi=ERC20_ABI)
    router = w3.eth.contract(address=UNISWAP_ROUTER, abi=ROUTER_ABI)
    quoter = w3.eth.contract(address=UNISWAP_QUOTER, abi=QUOTER_ABI)

    matic = w3.eth.get_balance(account.address)
    bridged_balance = bridged.functions.balanceOf(account.address).call()
    native_balance = native.functions.balanceOf(account.address).call()

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Wallet: {account.address}")
    print(f"Recipient: {recipient}")
    print(f"POL gas balance: {w3.from_wei(matic, 'ether')}")
    print(f"USDC.e bridged balance: {format_token(bridged_balance, USDC_DECIMALS)}")
    print(f"Native USDC balance: {format_token(native_balance, USDC_DECIMALS)}")

    if bridged_balance:
        min_out = bridged_balance * (10_000 - MAX_SLIPPAGE_BPS) // 10_000
        print(f"Planned swap: {format_token(bridged_balance, USDC_DECIMALS)} USDC.e -> native USDC")
        print(f"Minimum native USDC out: {format_token(min_out, USDC_DECIMALS)}")

        quote = quoter.functions.quoteExactInputSingle(
            {
                "tokenIn": BRIDGED_USDCE,
                "tokenOut": NATIVE_USDC,
                "amountIn": bridged_balance,
                "fee": 100,
                "sqrtPriceLimitX96": 0,
            }
        ).call()
        print(f"Quoted native USDC out: {format_token(quote[0], USDC_DECIMALS)}")

        allowance = bridged.functions.allowance(account.address, UNISWAP_ROUTER).call()
        if allowance < bridged_balance:
            print("Approval needed for Uniswap router")
            if args.execute:
                nonce = w3.eth.get_transaction_count(account.address)
                approve_tx = bridged.functions.approve(UNISWAP_ROUTER, bridged_balance).build_transaction(
                    {**build_tx_base(w3, account.address, nonce), "gas": 100000}
                )
                send_and_wait(w3, account, private_key, approve_tx)
                time.sleep(2)
        else:
            print("Existing router allowance is sufficient")

        params = {
            "tokenIn": BRIDGED_USDCE,
            "tokenOut": NATIVE_USDC,
            "fee": 100,
            "recipient": account.address,
            "amountIn": bridged_balance,
            "amountOutMinimum": min_out,
            "sqrtPriceLimitX96": 0,
        }

        gas_estimate = None
        if allowance >= bridged_balance or args.execute:
            gas_estimate = router.functions.exactInputSingle(params).estimate_gas({"from": account.address})
            print(f"Swap gas estimate: {gas_estimate}")
        else:
            print("Swap gas estimate skipped until approval is broadcast")
        if args.execute:
            nonce = w3.eth.get_transaction_count(account.address)
            swap_tx = router.functions.exactInputSingle(params).build_transaction(
                {**build_tx_base(w3, account.address, nonce), "gas": int((gas_estimate or 250000) * 1.25)}
            )
            send_and_wait(w3, account, private_key, swap_tx)
            time.sleep(2)
            native_balance = native.functions.balanceOf(account.address).call()

    if native_balance == 0:
        print("No native USDC available to send after swap planning.")
        return

    print(f"Planned send: {format_token(native_balance, USDC_DECIMALS)} native USDC -> {recipient}")
    if args.execute:
        nonce = w3.eth.get_transaction_count(account.address)
        transfer_tx = native.functions.transfer(recipient, native_balance).build_transaction(
            {**build_tx_base(w3, account.address, nonce), "gas": 100000}
        )
        send_and_wait(w3, account, private_key, transfer_tx)

        final_bridged = bridged.functions.balanceOf(account.address).call()
        final_native = native.functions.balanceOf(account.address).call()
        print("Final wallet token balances:")
        print(f"USDC.e bridged: {format_token(final_bridged, USDC_DECIMALS)}")
        print(f"Native USDC: {format_token(final_native, USDC_DECIMALS)}")


if __name__ == "__main__":
    main()
