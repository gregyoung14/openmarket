#!/usr/bin/env python3
"""Generate Polymarket bridge deposit addresses for a wallet."""

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.wallet_env import get_account  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", help="Wallet address. Defaults to the configured local wallet.")
    parser.add_argument("--output", default="/tmp/polymarket_deposit_address.txt")
    args = parser.parse_args()

    wallet_address = args.address or get_account().address
    url = "https://bridge.polymarket.com/deposit"

    print("=" * 60)
    print("GENERATING POLYMARKET DEPOSIT ADDRESS")
    print("=" * 60)
    print(f"Wallet: {wallet_address}")

    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        data=json.dumps({"address": wallet_address}),
        timeout=30,
    )
    print(f"Response status: {response.status_code}")
    response.raise_for_status()

    deposit_info = response.json()
    addresses = deposit_info.get("address", {})
    print(json.dumps(addresses, indent=2))

    evm_address = addresses.get("evm")
    if evm_address:
        Path(args.output).write_text(evm_address, encoding="utf-8")
        print(f"EVM deposit address saved to {args.output}")


if __name__ == "__main__":
    main()
