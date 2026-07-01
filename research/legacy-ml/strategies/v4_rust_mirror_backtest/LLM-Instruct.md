# V4: Rust-Mirror Backtest

## Overview

Second-generation backtester that **exactly mirrors the live Rust execution engine config** from `execution-engine/src/config.rs`. Key improvements: rolling 30-prediction signal window, consistency filter, 20% drawdown circuit breaker, $0.99 max entry price cap.

**File:** `backtest.py`
**Status:** Superseded by v5's tighter Alpha thresholds

## Architecture

Same ML pipeline as v3 (stacking ensemble), but the **signal aggregation and execution logic** now mirrors the Rust engine:

```
Same ensemble as v3
        |
build_market_signals() — NOW with rolling window + consistency
        |
    ┌─────────────────────────────────────┐
    │  RUST CONFIG MIRRORING:             │
    │  - Rolling last 30 predictions      │
    │  - MIN_CONFIDENCE = 0.50            │
    │  - MIN_CONSISTENCY = 0.00           │
    │  - Entry window: 0-300s             │
    │  - 20% drawdown circuit breaker     │
    │  - $0.99 max entry price            │
    └─────────────────────────────────────┘
        |
    Hold-to-Resolve + Momentum + Portfolio
```

## Key Config Changes vs v3

| Config | v3 | v4 (Rust) | Notes |
|--------|-----|-----------|-------|
| MIN_CONFIDENCE | 0.55 | **0.50** | Lower threshold, more trades |
| MIN_CONSISTENCY | N/A | **0.00** | Disabled (Rust had it at 0) |
| SIGNAL_WINDOW | All | **30** | Rolling window of last 30 preds |
| MIN_SECS_INTO_MARKET | 0 | **0** | Can trade at market open |
| MAX_SECS_INTO_MARKET | 300 | **300** | 5-min max entry |
| MAX_DAILY_LOSS_PCT | N/A | **0.20** | 20% circuit breaker |
| MAX_ENTRY_PRICE | N/A | **0.99** | Cap on entry price |
| WIN_THRESHOLD | N/A | **0.90** | Price > $0.90 = win |

## Signal Aggregation

The key difference from v3 is how 1s predictions are aggregated:

```python
# v3 (simple mean of ALL predictions):
mean_up_prob = probs.mean()

# v4 (rolling window — mirrors Rust SignalAggregator):
recent_probs = probs[-SIGNAL_WINDOW:]  # Last 30 preds only
mean_up_prob = recent_probs.mean()

# Consistency check:
if mean_up_prob > 0.5:
    consistency = (recent_probs > 0.5).mean()  # % of preds that agreed
else:
    consistency = (recent_probs <= 0.5).mean()
```

## Strategies

### Hold-to-Resolve (confidence sweep)
- Sweep from 0.50 to 0.95 in 1% steps
- Circuit breaker halts trading at 20% drawdown

### Momentum (TP sweep)
- Take-profit targets: 5%, 10%, 15%, 20%
- Falls back to resolution if TP not hit

### Multi-Strategy Portfolio
- Runs 4 strategies simultaneously with separate bankrolls:
  - VOLUME (Hold conf>50%)
  - QUALITY (Hold conf>64%)
  - SNIPER (Hold conf>71%)
  - MOMENTUM (TP=10% conf>50%)
- $100 per strategy = $400 total capital
- Reports individual + combined portfolio performance

## How to Run

```bash
python backtest.py
```

**Outputs:**
- `backtest_v2_results.html` — Strategy equity curves + trade log
- `portfolio_results.html` — Multi-strategy portfolio chart
- Console: Full confidence sweep table, strategy rankings

## Relationship to Rust Engine

**Direct 1:1 mapping.** Every config parameter matches the Rust execution engine:

```
backtest.py CONFIG         →  execution-engine/src/config.rs
─────────────────────────────────────────────────────────────
MIN_CONFIDENCE = 0.50      →  min_confidence: 0.50
MIN_CONSISTENCY = 0.00     →  min_consistency: 0.00
SIGNAL_WINDOW = 30         →  signal_window: 30
MIN_SECS_INTO_MARKET = 0   →  min_secs_into_market: 0
MAX_SECS_INTO_MARKET = 300 →  max_secs_into_market: 300
MAX_ENTRY_PRICE = 0.99     →  max_entry_price: 0.99
MAX_DAILY_LOSS_PCT = 0.20  →  max_daily_loss_pct: 0.20
BET_FRACTION = 0.05        →  bet_fraction: 0.05
SLIPPAGE = 0.005           →  slippage: 0.005
FEE_RATE = 0.01            →  fee_rate: 0.01
```

### Integration with Rust Engine

The Rust engine uses `ml_bridge.py` as the Python-side ML inference layer:

```bash
# Pipe mode (stdin/stdout JSON lines):
./rust_ws_ingest | python ml_bridge.py | ./rust_executor

# TCP socket mode:
python ml_bridge.py --mode tcp --port 9999
```

**Input format** (JSON lines from Rust → Python):
```json
{"type":"binance","T":1770895800123,"p":68150.25,"q":0.003,"m":false}
{"type":"poly","side":"UP","price":0.45,"bid":0.44,"ask":0.46,"size":100.5,"ts":1770895800456,"slug":"btc-updown-15m-1770895800"}
```

**Output format** (JSON lines from Python → Rust):
```json
{"type":"prediction","direction":"DOWN","confidence":0.673,"raw_prob":0.327,"timestamp":1770895830000}
{"type":"entry","action":"ENTER","side":"DOWN","confidence":0.71,"consistency":0.87,"entry_ask":0.545,"entry_price":0.550,"market":"btc-updown-15m-1770895800"}
```

## Key Takeaways

1. **Rolling window significantly changes results** — the last 30 predictions are more reactive than the full-window mean
2. **Circuit breaker prevents catastrophic losses** — halts trading before bankroll destruction
3. **MIN_CONFIDENCE = 0.50 generates too many trades** — many are low-conviction noise, which is why v5 raised it
4. **Consistency filter at 0.0 means it's effectively disabled** — v5 adds it back
