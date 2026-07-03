# Polymarket BTC Trading Platform — Documentation

## Navigation

### [Architecture Overview](./architecture/overview.md)

Core system components, data flow, and design decisions.

### [Service Catalog](./architecture/services.md)

All 6 services: ports, endpoints, responsibilities.

### Setup

- **[VPS Configuration](./setup/vps_config.md)** — OS, storage layout, firewall
- **[Installation](./setup/installation.md)** — Cloning, Rust/Python setup, environment variables
- **[Dependencies](./setup/dependencies.md)** — Rust, Python, and frontend dependency management

### Authentication

- **[Credentials](./authentication/credentials.md)** — L1 (private key) and L2 (API keys)
- **[Wallet Utilities](./authentication/wallet_gen.md)** — `wallet-gen/` derivation and swap tools

### Trading

- **[Execution](./trading/execution.md)** — Signal flow, order placement, risk management
- **[Position Redemption](./trading/redemption.md)** — On-chain redemption, trade ledger, dashboard
- **[Paper Tournament Profitability Path (2026-04-05)](./TDR-paper-tournament-profitability-path-2026-04-05.md)** — v2 canary analysis, failure modes, and next-step strategy plan
- **[Calibrated Net-EV Binary Scorer (2026-04-05)](./TDR-calibrated-net-ev-binary-scorer-2026-04-05.md)** — symmetric probability model, calibration path, and EV-based candidate scoring design
- **[Calibrated Net-EV Delivery Task List (2026-04-06)](./TASKLIST-calibrated-net-ev-binary-scorer-2026-04-06.md)** — phased implementation backlog, dependencies, validation gates, and rollout order
- **[Calibrated Net-EV Feature Contract (2026-04-06)](./FEATURE-CONTRACT-calibrated-net-ev-binary-scorer-2026-04-06.md)** — rollout modes, dataset schema, runtime artifact location, and paper-only scorer fields

### Operations

- **[Monitoring & Logs](./monitoring/logs.md)** — Log locations, systemd commands, health checks
- **[Paper Tournament Stall Postmortem (2026-04-03)](./monitoring/paper-tournament-stall-2026-04-03.md)** — Root cause, recovery timeline, and follow-up actions

### Reference

- **[Version Management](../rust-services/btc-common/VERSION_GUIDE.md)** — How to bump signal version

---

## Current Status (March 2026)

| Component | Status |
| --- | --- |
| **Signal Engine** | `v14` — pure Rust drift estimator (port 8003) |
| **Execution Engine** | Live trading on Polymarket CLOB (port 8004) |
| **Redeem Positions** | Automated on-chain redemption + trade ledger (port 8006) |
| **Binance WS** | Real-time BTC stream (port 8001) |
| **Polymarket WS** | Real-time CLOB data (port 8002) |
| **Market Recorder** | Multi-market Polymarket subscription + ML dataset export (port 8005) |

### Codebase Notes

- **All runtime services are Rust** except `redeem-positions` (Python).
- **No legacy Python services remain** — all original Python trading/scraping code was replaced by Rust services and removed (March 2026).
- **Signal Engine v1 archive** (ML bridge via TCP/Python) was removed — it's preserved in git history before commit `f4d107b`.
- **One-time backfill scripts** live in `scripts/deprecated/` for reference only.
