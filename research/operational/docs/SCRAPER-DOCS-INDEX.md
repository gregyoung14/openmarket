# Polymarket BTC Trading Platform — Documentation

## Navigation

This index covers the operational documents actually preserved in
`research/operational/docs/`. Empty category directories are retained only as
historical placeholders; missing private runbooks were not recreated.

### Architecture And Execution

- **[Execution Engine v15 Spec](./EXECUTION-ENGINE-V15-SPEC.md)** — execution parser, sizing, signal flow, and mismatch guards.
- **[Polymarket Inversion Root Cause](./POLYMARKET-INVERSION-ROOT-CAUSE.md)** — token-ordering bug analysis.
- **[Polymarket Audit](./TDR-POLYMARKET-AUDIT.md)** — paper-executor integration audit against Polymarket docs.
- **[Rolling BTC Context](./TDR-ROLLING-BTC-CONTEXT.md)** — signal-engine rolling context design.
- **[Backtest Database Access](./TDR-backtest-database-access.md)** — archived snapshot access model.

### Trading

- **[Paper Tournament Profitability Path (2026-04-05)](./TDR-paper-tournament-profitability-path-2026-04-05.md)** — v2 canary analysis, failure modes, and next-step strategy plan
- **[Calibrated Net-EV Binary Scorer (2026-04-05)](./TDR-calibrated-net-ev-binary-scorer-2026-04-05.md)** — symmetric probability model, calibration path, and EV-based candidate scoring design
- **[Calibrated Net-EV Delivery Task List (2026-04-06)](./TASKLIST-calibrated-net-ev-binary-scorer-2026-04-06.md)** — phased implementation backlog, dependencies, validation gates, and rollout order
- **[Calibrated Net-EV Feature Contract (2026-04-06)](./FEATURE-CONTRACT-calibrated-net-ev-binary-scorer-2026-04-06.md)** — rollout modes, dataset schema, runtime artifact location, and paper-only scorer fields
- **[Mobile Paper Tournament Dashboard](./TDR-mobile-paper-tournament-dashboard.md)** — read-only mobile dashboard design.

### Operations

- **[Ledger Version Correlation Audit (2026-03-09)](./LEDGER-VERSION-CORRELATION-AUDIT-2026-03-09.md)** — git/runtime/ledger reconciliation.

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

- The archived tree contains 8 standalone Rust service directories and 2 Python service directories.
- Runtime/output data such as private ledgers, paper logs, wallet state, and raw credentials are intentionally omitted or sanitized.
- Empty documentation category directories are placeholders from the source repo; this index only links files present in the archive.
