#!/usr/bin/env python3
"""
Check best available route for Polygon USDC(native) <-> USDC.e conversion.

Checks:
1) Uniswap V3 exactInputSingle gas estimation across fee tiers
2) 0x swap quote availability
3) Stargate quotes (same-chain and reference cross-chain)

This script is read-only and does not send transactions.
"""

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from web3 import Web3


RPC_URL = "https://polygon-rpc.com"
NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
BRIDGED_USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
UNISWAP_ROUTER_V3 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
FEE_TIERS = [100, 500, 3000, 10000]

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


@dataclass
class CheckResult:
    engine: str
    ok: bool
    details: str
    meta: Optional[Dict[str, Any]] = None


def load_wallet_address(private_key_path: str) -> str:
    with open(private_key_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if content.startswith("PRIVATE_KEY="):
        content = content.split("=", 1)[1]
    return Web3().eth.account.from_key(content).address


def check_uniswap(
    w3: Web3,
    wallet_address: str,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> List[CheckResult]:
    router = w3.eth.contract(address=UNISWAP_ROUTER_V3, abi=ROUTER_ABI)
    results: List[CheckResult] = []

    for fee in FEE_TIERS:
        params = {
            "tokenIn": token_in,
            "tokenOut": token_out,
            "fee": fee,
            "recipient": wallet_address,
            "amountIn": amount_in,
            "amountOutMinimum": 1,
            "sqrtPriceLimitX96": 0,
        }
        try:
            gas = router.functions.exactInputSingle(params).estimate_gas({"from": wallet_address})
            results.append(
                CheckResult(
                    engine=f"uniswap_v3_fee_{fee}",
                    ok=True,
                    details=f"gas estimate ok: {gas}",
                    meta={"gasEstimate": gas, "fee": fee},
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    engine=f"uniswap_v3_fee_{fee}",
                    ok=False,
                    details=str(exc),
                    meta={"fee": fee},
                )
            )
    return results


def check_0x(wallet_address: str, sell_token: str, buy_token: str, sell_amount: int) -> CheckResult:
    url = "https://polygon.api.0x.org/swap/v1/quote"
    params = {
        "buyToken": buy_token,
        "sellToken": sell_token,
        "sellAmount": str(sell_amount),
        "takerAddress": wallet_address,
        "slippagePercentage": "0.01",
    }
    try:
        response = requests.get(url, params=params, timeout=20)
        data = response.json()
        if response.status_code == 200 and "buyAmount" in data:
            return CheckResult(
                engine="0x",
                ok=True,
                details=f"buyAmount={data.get('buyAmount')}",
                meta={"quote": data},
            )
        return CheckResult(engine="0x", ok=False, details=data.get("message", response.text), meta={"status": response.status_code})
    except Exception as exc:
        return CheckResult(engine="0x", ok=False, details=str(exc))


def check_stargate_same_chain(wallet_address: str, src_token: str, dst_token: str, src_amount: int) -> CheckResult:
    url = "https://stargate.finance/api/v1/quotes"
    params = {
        "srcToken": src_token,
        "dstToken": dst_token,
        "srcAddress": wallet_address,
        "dstAddress": wallet_address,
        "srcChainKey": "polygon",
        "dstChainKey": "polygon",
        "srcAmount": str(src_amount),
        "dstAmountMin": str(int(src_amount * 0.99)),
    }
    try:
        response = requests.get(url, params=params, timeout=25)
        data = response.json()
        quotes = data.get("quotes", [])
        if response.status_code == 200 and quotes:
            return CheckResult(engine="stargate_same_chain", ok=True, details=f"quotes={len(quotes)}", meta={"quotes": quotes})
        return CheckResult(
            engine="stargate_same_chain",
            ok=False,
            details=(data.get("error", {}) or {}).get("message", "no route"),
            meta={"status": response.status_code, "body": data},
        )
    except Exception as exc:
        return CheckResult(engine="stargate_same_chain", ok=False, details=str(exc))


def print_summary(results: List[CheckResult]) -> None:
    print("\n=== Route Check Summary ===")
    for result in results:
        emoji = "✅" if result.ok else "❌"
        print(f"{emoji} {result.engine}: {result.details}")

    best = [result for result in results if result.ok]
    if not best:
        print("\nNo executable route found for this token pair and amount.")
        return

    print("\nViable routes found:")
    for result in best:
        print(f"- {result.engine}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check best USDC(native) <-> USDC.e route on Polygon")
    parser.add_argument(
        "direction",
        choices=["native-to-bridged", "bridged-to-native"],
        help="Conversion direction",
    )
    parser.add_argument("--amount", type=float, default=1.0, help="Amount in token units (default: 1.0)")
    parser.add_argument(
        "--private-key-path",
        type=str,
        default="/path/to/wallet-gen/.env.wallet",
        help="Path to file containing PRIVATE_KEY=...",
    )
    parser.add_argument("--json", action="store_true", help="Print full machine-readable JSON output")
    args = parser.parse_args()

    wallet_address = load_wallet_address(args.private_key_path)
    amount_in = int(args.amount * 1_000_000)

    if args.direction == "native-to-bridged":
        token_in, token_out = NATIVE_USDC, BRIDGED_USDCE
    else:
        token_in, token_out = BRIDGED_USDCE, NATIVE_USDC

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Failed to connect to Polygon RPC")

    print(f"Wallet: {wallet_address}")
    print(f"Direction: {args.direction}")
    print(f"Amount: {args.amount}")

    results: List[CheckResult] = []
    results.extend(check_uniswap(w3, wallet_address, token_in, token_out, amount_in))
    results.append(check_0x(wallet_address, token_in, token_out, amount_in))
    results.append(check_stargate_same_chain(wallet_address, token_in, token_out, amount_in))

    print_summary(results)

    if args.json:
        encoded = [
            {
                "engine": r.engine,
                "ok": r.ok,
                "details": r.details,
                "meta": r.meta,
            }
            for r in results
        ]
        print("\n=== JSON ===")
        print(json.dumps(encoded, indent=2, default=str))


if __name__ == "__main__":
    main()
