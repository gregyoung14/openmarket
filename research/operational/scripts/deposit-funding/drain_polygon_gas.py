#!/usr/bin/env python3
"""Drain remaining native Polygon gas token (POL/MATIC) to a deposit address.

The script sends the full native token balance minus the estimated fee for this
final transfer. It defaults to dry-run mode; pass --execute to broadcast.
"""

import argparse
import sys
from pathlib import Path

from web3 import Web3

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.wallet_env import connect_polygon, get_account, get_private_key  # noqa: E402


GAS_LIMIT = 21000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("deposit_address", help="POL/MATIC Polygon deposit address")
    parser.add_argument("--execute", action="store_true", help="Broadcast the transfer")
    args = parser.parse_args()

    private_key = get_private_key()
    account = get_account()
    recipient = Web3.to_checksum_address(args.deposit_address)
    w3 = connect_polygon()

    balance = w3.eth.get_balance(account.address)
    gas_price = w3.eth.gas_price
    fee = GAS_LIMIT * gas_price
    value = balance - fee - gas_price

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Wallet: {account.address}")
    print(f"Recipient: {recipient}")
    print(f"Native gas balance: {w3.from_wei(balance, 'ether')}")
    print(f"Gas price wei: {gas_price}")
    print(f"Estimated fee: {w3.from_wei(fee, 'ether')}")

    if value <= 0:
        raise RuntimeError("Balance is too low to drain after estimated gas")

    print(f"Planned send: {w3.from_wei(value, 'ether')}")

    if not args.execute:
        return

    tx = {
        "to": recipient,
        "value": value,
        "gas": GAS_LIMIT,
        "gasPrice": gas_price,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 137,
    }
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = w3.to_hex(tx_hash)
    print(f"Broadcast: {tx_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction failed: {tx_hex}")
    print(f"Confirmed in block {receipt['blockNumber']}")
    print(f"Final native gas balance: {w3.from_wei(w3.eth.get_balance(account.address), 'ether')}")


if __name__ == "__main__":
    main()
