# Operational Reproduction Scripts

These scripts and services are copied from the original `polymarket-btc-scraper`
operational repo and are preserved here for researchers who want to reproduce
wallet, funding, balance-checking, and live-signal steps. They are **not** part
of the core OpenMarket research archive; they are optional operational tooling.

> **Warning.** These scripts perform real on-chain actions (deposits, withdrawals,
> trades, order cancellations) when supplied with live credentials. They are
> provided as-is for reproducibility and auditing; do not run them with real
> funds unless you understand every step.

## Layout

```text
research/operational/
├── scripts/
│   ├── balance-info/          # Check on-chain and Polymarket balances
│   ├── deposit-funding/       # Deposit, withdraw, approve, and bridge USDC
│   ├── trading/               # Place, monitor, and cancel Polymarket trades
│   ├── common/
│   │   └── wallet_env.py      # Shared wallet/Polymarket credential loader
│   └── requirements.txt       # Python dependencies
└── rust-services/
    ├── btc-common/            # Version constants shared by trading services
    └── signal-engine/         # Live drift-estimator signal service
```

## Credentials

All scripts load secrets from environment variables or ignored env files:

- `POLYGON_PRIVATE_KEY` or `PRIVATE_KEY` — EOA private key
- `POLYMARKET_API_KEY`
- `POLYMARKET_SECRET`
- `POLYMARKET_PASSPHRASE`

See `scripts/common/wallet_env.py` for the loader implementation.

## Python setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r research/operational/scripts/requirements.txt
```

## Rust signal-engine setup

The operational signal engine is intentionally **not** part of the main workspace
so it does not affect `cargo check --workspace`. Build it standalone:

```bash
cd research/operational/rust-services/signal-engine
cargo build --release
```

## Sanitization note

All personal identifiers, wallet addresses, IP addresses, and infrastructure
hostnames have been removed or generalized. Public smart-contract addresses,
Polymarket/Binance API endpoints, and well-known RPC URLs are retained because
they are required for reproduction and are not personal data.
