#!/usr/bin/env python3
"""Send native Polygon USDC or bridged USDC.e to an address.

Default mode is dry-run. Pass --execute to broadcast.
"""

import argparse
import sys
from pathlib import Path

from web3 import Web3

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.wallet_env import (  # noqa: E402
    BRIDGED_USDCE,
    ERC20_ABI,
    NATIVE_USDC,
    USDC_DECIMALS,
    connect_polygon,
    format_token,
    get_account,
    get_private_key,
)


TOKEN_ADDRESSES = {
    "native": NATIVE_USDC,
    "bridged": BRIDGED_USDCE,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipient", help="Destination Polygon address")
    parser.add_argument("--token", choices=TOKEN_ADDRESSES, default="bridged")
    parser.add_argument("--amount", type=float, help="Amount to send. Defaults to full token balance.")
    parser.add_argument("--execute", action="store_true", help="Broadcast transaction")
    args = parser.parse_args()

    private_key = get_private_key()
    account = get_account()
    recipient = Web3.to_checksum_address(args.recipient)
    w3 = connect_polygon()
    token = w3.eth.contract(address=TOKEN_ADDRESSES[args.token], abi=ERC20_ABI)
    balance = token.functions.balanceOf(account.address).call()
    amount = balance if args.amount is None else int(args.amount * (10**USDC_DECIMALS))

    if amount <= 0:
        raise RuntimeError("No token amount to send")
    if amount > balance:
        raise RuntimeError(f"Insufficient balance: {format_token(balance)} available")

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Wallet: {account.address}")
    print(f"Recipient: {recipient}")
    print(f"Token: {args.token}")
    print(f"Balance: {format_token(balance)}")
    print(f"Planned send: {format_token(amount)}")

    if not args.execute:
        return

    tx = token.functions.transfer(recipient, amount).build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        }
    )
    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    tx_hex = w3.to_hex(tx_hash)
    print(f"Broadcast: {tx_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction failed: {tx_hex}")
    print(f"Confirmed in block {receipt['blockNumber']}")


if __name__ == "__main__":
    main()
