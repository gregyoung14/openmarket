# Operational Reproduction Archive

These scripts, services, and docs are copied from the original
`polymarket-btc-scraper` operational repo and are preserved here so researchers
 can reproduce the full production-grade automated trading platform:
real-time WebSocket feeds, signal generation, execution, paper tournaments,
on-chain redemption, auditing, and deployment.

> **Warning.** These components perform real on-chain actions (deposits,
> withdrawals, trades, order cancellations) when supplied with live credentials.
> They are provided for reproducibility and auditing only. Do not run them with
> real funds unless you understand every step.

## What is here

### Rust services (`rust-services/`)

High-performance Tokio/Axum services from the live system:

| Service | Purpose |
|---|---|
| `binance-websocket` | Real-time Binance BTC/USDT trade/book feed |
| `polymarket-websocket` | Real-time Polymarket CLOB feed (includes inversion-root-cause fixes) |
| `market-data-recorder` | 100ms/1s tick recording, candles, lag pairs, ML step2 exports |
| `signal-engine` | Drift/OFI/volume scanner, state management, calibrated scoring (v14+) |
| `execution-engine` | CLOB order placement, position management, risk controls |
| `paper-executor` | Paper trading simulation with sizing, fees, slippage |
| `db-backup` | SQLite snapshot offload to BunnyCDN |
| `btc-common` | Shared version constants |

These are intentionally **outside** the main Cargo workspace. Build them
standalone:

```bash
cd research/operational/rust-services/signal-engine
cargo build --release
```

### Python services (`services/`)

| Service | Purpose |
|---|---|
| `paper-tournament/` | Launcher + HTML dashboard for comparing strategy variants |
| `redeem-positions/` | On-chain USDC redemption service + dashboard |

### Scripts (`scripts/`)

| Directory | Purpose |
|---|---|
| `balance-info/` | Check on-chain and Polymarket balances |
| `deposit-funding/` | Deposit, withdraw, approve, bridge, and swap USDC |
| `trading/` | Place, monitor, and cancel Polymarket trades |
| `wallet-setup/` | Derive credentials, create API keys, swap via Uniswap |
| `audit/` | Ledger/on-chain reconciliation, version backfills, retro fixes |
| `monitoring/` | Health checks, storage guardrails, service info |
| `services/` | Start scripts for WS services, recorder, overlay frontend |
| `deployment/` | VPS deployment template and health check |
| `database/` | Backup helper and DB init |
| `common/` | Shared `wallet_env.py` credential loader |

Install Python dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r research/operational/scripts/requirements.txt
```

### Docs (`docs/`)

Key TDRs and operational guides:

- `POLYMARKET-INVERSION-ROOT-CAUSE.md` — the token-ordering bug that inverted trades
- `TDR-paper-tournament-profitability-path-2026-04-05.md` — empirical discovery that cheap contracts are the trap
- `TDR-calibrated-net-ev-binary-scorer-2026-04-05.md` — proposal to replace heuristic edge with calibrated EV
- `FEATURE-CONTRACT-*` and `TASKLIST-*` — feature contract and implementation plan
- `EXECUTION-ENGINE-V15-SPEC.md` — v15 execution engine spec
- `architecture/overview.md` and `architecture/services.md` — system architecture
- `trading/execution.md`, `trading/redemption.md`, `trading/wallet-operations.md`
- `setup/`, `authentication/`, `monitoring/` — install, credentials, logs
- `official-docs-mcp/` — MCP-formal design and audit documents

### Deployment (`systemd/`)

User systemd units:

- `db-backup.service`
- `storage-guardrails.service`
- `storage-guardrails.timer`

## Credentials

All scripts load secrets from environment variables or ignored env files:

- `POLYGON_PRIVATE_KEY` or `PRIVATE_KEY` — EOA private key
- `POLYMARKET_API_KEY`
- `POLYMARKET_SECRET`
- `POLYMARKET_PASSPHRASE`

See `scripts/common/wallet_env.py` for the loader implementation.

## Sanitization note

Host-specific paths (`/home/ec2-user/`, `/mnt/nvme/`), the BunnyCDN storage
zone name, and infrastructure identifiers have been replaced with generic
placeholders. Public smart-contract addresses, Polymarket/Binance API
endpoints, and well-known RPC URLs are retained because they are required for
reproduction and are not personal data. Any remaining hardcoded secrets in the
original files (e.g., test-only Anvil keys, deployment access tokens) have been
replaced with placeholders.
