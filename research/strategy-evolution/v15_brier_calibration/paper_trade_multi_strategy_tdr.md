# TDR: Paper-Trade Multi-Strategy Runner

**Date:** 2026-03-14
**Status:** Proposed
**Author:** Greg

---

## 1. Problem Statement

v14 is live on Polymarket but underperforming backtest expectations (52.6% WR live vs 76.2% backtest, 19 trades over many days vs ~26/day expected). We have multiple candidate strategy variants (v14 baseline, v14.1 vol-gate-off, v15 Brier CB) but no way to validate them against live market conditions without risking capital.

**Goal:** Run up to 10 strategy variants simultaneously in paper-trade mode against live Polymarket data to measure real-world win rates, trade frequency, and signal quality — without placing any real orders.

---

## 2. Current Architecture

Six Rust microservices, all on localhost:

| Service              | Port | Role                                         |
|----------------------|------|----------------------------------------------|
| binance-websocket    | 8001 | BTC/USDT real-time price stream              |
| polymarket-websocket | 8002 | CLOB orderbook, bid/ask, trades              |
| signal-engine        | 8003 | v14 drift estimator → entry signals via WS   |
| execution-engine     | 8004 | Kelly sizing → limit orders → position mgmt  |
| market-data-recorder | 8005 | SQLite recording + ML feature export         |
| redeem-positions     | 8006 | On-chain settlement (Python)                 |

**Data flow:**
```
Binance WS (8001) ──┐
                     ├──▶ signal-engine (8003) ──▶ execution-engine (8004) ──▶ Polymarket CLOB
Polymarket WS (8002)─┘
```

Signal-engine computes drift every 1s, fires entry when adaptive confirmation window (15–50s) is sustained. Execution-engine consumes entries, applies Kelly sizing (half-Kelly, clamped 1–5%), places GTC limit orders via Polymarket SDK.

**Existing `TEST_MODE`:** Sets fixed 1-share sizing and relaxes minimum balance check — but still places real orders on live CLOB. Not paper trading.

---

## 3. Proposed Design

### 3.1 Shared Data Layer (No Changes)

The data infrastructure is stateless and shared. One instance of each serves all strategies:

| Component            | Instances | Change Required |
|----------------------|-----------|-----------------|
| binance-websocket    | 1         | None            |
| polymarket-websocket | 1         | None            |
| market-data-recorder | 1         | None            |

### 3.2 Multiple Signal Engines

Each strategy variant runs its own signal-engine instance on a unique port. All signal-engine parameters are already env-var-tunable:

- `W_DRIFT`, `W_OFI_ACCEL`, `W_SCOREBOARD`, `WHIPSAW_WEIGHT`
- `REGIME_CHOP_THRESHOLD`, `REGIME_AUTOCORR_CHOP`
- `BASE_CONFIRM_WINDOW`, `MIN_CONFIRM_WINDOW`, `MAX_CONFIRM_WINDOW`
- `ENABLE_VOLUME_GATE`
- `THETA_WEIGHT`, `VEGA_WEIGHT`

**Only change needed:** Parameterize the listen port via `SIGNAL_PORT` env var (currently hardcoded to 8003).

Example — 3 strategies running simultaneously:
```bash
# Strategy A: v14 baseline
SIGNAL_PORT=8010 cargo run --release &

# Strategy B: v14.1 volume gate off
SIGNAL_PORT=8011 ENABLE_VOLUME_GATE=false cargo run --release &

# Strategy C: v15 relaxed thresholds + no vol gate
SIGNAL_PORT=8012 ENABLE_VOLUME_GATE=false MIN_CONFIDENCE=0.56 MIN_EDGE=0.05 cargo run --release &
```

### 3.3 Paper-Trade Executor (New Component)

Replace the live execution-engine with a lightweight paper-trade logger per strategy:

```
┌─────────────────────┐    ┌──────────────────────┐
│ signal-engine :8010  │───▶│ paper-executor :9010  │───▶ paper_log_A.csv
└─────────────────────┘    └──────────────────────┘
┌─────────────────────┐    ┌──────────────────────┐
│ signal-engine :8011  │───▶│ paper-executor :9011  │───▶ paper_log_B.csv
└─────────────────────┘    └──────────────────────┘
         ...                        ...
```

**Paper executor responsibilities:**
1. Connect to its assigned signal-engine WS
2. Connect to polymarket-websocket (8002) for live bid/ask prices
3. On entry signal: log trade with timestamp, direction, confidence, edge, entry ask — **no order placed**
4. Track simulated bankroll with Kelly sizing (same formula as live)
5. On market resolution: look up result, compute PnL, update bankroll
6. Write all trades to per-strategy CSV

**What it does NOT do:**
- Hit Polymarket CLOB REST API
- Touch any wallet or on-chain state
- Interact with the Polymarket SDK

**Estimated scope:** ~100–150 lines of Rust. The signal consumer, price feed, and Kelly math already exist in execution-engine — just remove the order submission and wallet calls.

### 3.4 Orchestration

A single shell script spins up the full stack:

```bash
#!/bin/bash
# start_paper_tournament.sh

# --- Shared data layer (already running as systemd services) ---
# binance-websocket :8001
# polymarket-websocket :8002
# market-data-recorder :8005

# --- Strategy variants ---
declare -A STRATEGIES=(
  ["v14_baseline"]="SIGNAL_PORT=8010"
  ["v14_1_no_volgate"]="SIGNAL_PORT=8011 ENABLE_VOLUME_GATE=false"
  ["v15_brier_cb"]="SIGNAL_PORT=8012 ENABLE_VOLUME_GATE=false BRIER_CB=true"
  ["v14_relaxed_conf"]="SIGNAL_PORT=8013 MIN_CONFIDENCE=0.56 MIN_EDGE=0.05"
  # ... up to 10
)

for name in "${!STRATEGIES[@]}"; do
  env ${STRATEGIES[$name]} ./signal-engine &
  ./paper-executor --signal-url ws://127.0.0.1:${port} --log ${name}.csv &
done
```

---

## 4. Resource Estimate

Per signal-engine instance:
- **RAM:** ~3KB (200B state × 16 active markets)
- **CPU:** Negligible (1s drift calc per market, pure math)
- **Network:** Read-only from shared WS connections
- **Disk:** ~1KB/trade in CSV logs

For 10 simultaneous strategies:
- **Total extra RAM:** ~30KB
- **Total extra CPU:** <1% of a single core
- **WS connections:** 10 additional localhost connections (trivial)
- **Disk:** ~10 CSV files, growing at trade rate

**Polymarket API rate limit (10 orders/s):** Irrelevant — paper executors never hit the CLOB.

---

## 5. Output & Analysis

Each paper executor writes a CSV per strategy:

```csv
timestamp,strategy,slug,direction,confidence,edge,regime,entry_ask,result,pnl,bankroll
1710432000000,v14_baseline,btc-updown-15m-1710432000,UP,0.72,0.12,Trend,0.505,WIN,0.88,101.76
```

Post-hoc analysis script compares all strategies on:
- Trade count / frequency
- Win rate
- Brier score (with recalibration from v15)
- PnL / ROI
- Max drawdown
- Alpha vs market (OLS regression)

---

## 6. Implementation Plan

| Step | Description                                          | Effort |
|------|------------------------------------------------------|--------|
| 1    | Parameterize signal-engine port via `SIGNAL_PORT` env var | 1 line |
| 2    | Build paper-executor binary (fork execution-engine, strip order submission) | ~150 LOC |
| 3    | Add market resolution tracking to paper-executor (poll resolved prices) | ~50 LOC |
| 4    | Write orchestration script (`start_paper_tournament.sh`) | ~30 LOC |
| 5    | Write analysis script (compare CSVs, compute metrics) | ~100 LOC Python |
| 6    | Deploy to VPS, run for 1 week | Ops |
| 7    | Analyze results, pick winner for live deployment | Analysis |

**Total new code:** ~330 lines (Rust + Python + shell)

---

## 7. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Paper fills ≠ live fills (slippage, spread widening) | Paper WR overstates live WR | Use conservative slippage model (entry_ask + 1¢ instead of 0.5¢) |
| Signal-engine instances compete for CPU during peak | Delayed signals | Unlikely — drift calc is ~1μs. 10 instances = ~10μs/s total |
| Market resolution data not available instantly | Delayed PnL calc | Paper executor polls resolution every 30s, acceptable lag |
| Polymarket WS disconnects affect all strategies | All paper trades miss data | Already handled by reconnect logic (50ms base, 2s max) |

---

## 8. Success Criteria

After 1 week of paper trading (~670 market windows per strategy):

- [ ] At least 5 strategies produce ≥50 trades for statistical significance
- [ ] Identify any strategy with WR ≥ 65% on ≥100 trades
- [ ] Measure backtest-to-paper WR gap per strategy
- [ ] Brier CB correctly pauses during cold streaks (if enabled)
- [ ] Pick top performer for live deployment with real capital

---

## 9. Decision

**Recommendation:** Build it. The shared data layer already exists, signal-engine is already env-var parameterized, and the paper executor is a thin ~150-line fork of execution-engine. Total effort is ~1 day of coding + 1 week of data collection. The value — validating 10 strategy variants against live market conditions without risking capital — is high relative to the cost.
