#!/usr/bin/env python3
"""
Check if wallet is ready to trade on Polymarket
"""
from web3 import Web3
from eth_account import Account

POLYGON_PRIVATE_KEY = "REPLACE_WITH_POLYGON_PRIVATE_KEY"

if POLYGON_PRIVATE_KEY.startswith("0x"):
    POLYGON_PRIVATE_KEY = POLYGON_PRIVATE_KEY[2:]

account = Account.from_key(POLYGON_PRIVATE_KEY)
my_address = account.address

# Connect to Polygon
rpc_url = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(rpc_url))

# Contract addresses
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens Framework
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CTF Exchange

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

usdc_contract = w3.eth.contract(
    address=Web3.to_checksum_address(USDC_E_ADDRESS),
    abi=erc20_abi
)

print("=" * 60)
print("POLYMARKET TRADING READINESS CHECK")
print("=" * 60)
print(f"\nWallet: {my_address}")

# Check USDC.e balance
usdc_wei = usdc_contract.functions.balanceOf(my_address).call()
decimals = usdc_contract.functions.decimals().call()
usdc_balance = usdc_wei / (10 ** decimals)
print(f"\n✅ USDC.e Balance: {usdc_balance} USDC")

# Check approvals
ctf_allowance = usdc_contract.functions.allowance(
    my_address,
    Web3.to_checksum_address(CTF_ADDRESS)
).call()
ctf_allowance_usdc = ctf_allowance / (10 ** decimals)

exchange_allowance = usdc_contract.functions.allowance(
    my_address,
    Web3.to_checksum_address(CTF_EXCHANGE)
).call()
exchange_allowance_usdc = exchange_allowance / (10 ** decimals)

print(f"\nAPPROVALS:")
print(f"  CTF Contract:      {ctf_allowance_usdc:,.2f} USDC {'✅' if ctf_allowance_usdc > 0 else '❌'}")
print(f"  CTF Exchange:      {exchange_allowance_usdc:,.2f} USDC {'✅' if exchange_allowance_usdc > 0 else '❌'}")

print("\n" + "=" * 60)
if usdc_balance > 0 and (ctf_allowance_usdc > 0 or exchange_allowance_usdc > 0):
    print("✅ READY TO TRADE!")
    print("=" * 60)
    print("\nYou can now:")
    print("  1. Browse markets: https://polymarket.com")
    print("  2. Place orders via API using py-clob-client")
    print("  3. Your funds stay in your wallet until orders fill")
else:
    print("⚠️  NOT READY")
    print("=" * 60)
    if usdc_balance == 0:
        print("❌ No USDC.e balance")
    if ctf_allowance_usdc == 0:
        print("⚠️  Need to approve CTF Contract")
    if exchange_allowance_usdc == 0:
        print("⚠️  Need to approve CTF Exchange")
