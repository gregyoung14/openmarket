# V5: Alpha Backtest (Premium Thresholds)

## Overview

Tightened version of v4 with **premium "Alpha" thresholds**: higher minimum confidence (0.60), active consistency filter (0.60), and delayed entry (wait 30s for signal buildup). This is the "quality over quantity" approach — fewer trades but higher conviction.

**File:** `backtest_alpha.py`
**Status:** Superseded by v6's drift approach, but still the production ML-based strategy

## Key Config Changes vs v4

| Config | v4 (Rust Default) | v5 (Alpha) | Why |
|--------|-------------------|------------|-----|
| MIN_CONFIDENCE | 0.50 | **0.60** | Filter out low-conviction noise |
| MIN_CONSISTENCY | 0.00 | **0.60** | Require 60%+ of predictions to agree |
| MIN_SECS_INTO_MARKET | 0 | **30** | Wait 30s for signal buildup |
| MAX_SECS_INTO_MARKET | 300 | **600** | Wider window to find signals |

Everything else (bankroll, bet fraction, slippage, fees, circuit breaker) remains identical to v4.

## Signal Method

Same as v4 but with stricter filters:

```python
# 1. Train ensemble on ALL data
xgb_m, lgb_m, meta_clf = train_ensemble(df_ml)

# 2. For each 15-min market:
#    a. Get rolling last 30 predictions
#    b. Average → direction + confidence
#    c. Check consistency (% of preds agreeing)
#    d. FILTER: confidence >= 0.60 AND consistency >= 0.60
#    e. FILTER: entry must be 30-600 seconds into market
```

## Strategies

### Hold-to-Resolve (confidence sweep)
- Sweep from 0.50 to 0.95
- With 20% drawdown circuit breaker

### Momentum (TP sweep)
- Take-profit targets: 5%, 10%, 15%, 20%

### Multi-Strategy Portfolio
- Same 4-strategy portfolio as v4
- $100 per strategy allocation

## How to Run

```bash
python backtest_alpha.py
```

**Outputs:**
- `backtest_alpha_results.html` — Equity curves + trade log
- `portfolio_alpha_results.html` — Portfolio report
- Console: Full sweep, rankings

## Relationship to Rust Engine

**Updated config mapping for Alpha Premium:**

```
backtest_alpha.py CONFIG       →  execution-engine/src/config.rs (Alpha)
──────────────────────────────────────────────────────────────────────
MIN_CONFIDENCE = 0.60         →  min_confidence: 0.60
MIN_CONSISTENCY = 0.60        →  min_consistency: 0.60
SIGNAL_WINDOW = 30            →  signal_window: 30
MIN_SECS_INTO_MARKET = 30     →  min_secs_into_market: 30
MAX_SECS_INTO_MARKET = 600    →  max_secs_into_market: 600
```

### Deploying Alpha Config to Rust

To switch the Rust engine to Alpha config, update `execution-engine/src/config.rs`:

```rust
pub struct TradingConfig {
    pub min_confidence: f64,      // 0.60
    pub min_consistency: f64,     // 0.60
    pub signal_window: usize,    // 30
    pub min_secs_into_market: u64, // 30
    pub max_secs_into_market: u64, // 600
    pub max_entry_price: f64,    // 0.99
    pub max_daily_loss_pct: f64, // 0.20
    pub bet_fraction: f64,       // 0.05
    pub slippage: f64,           // 0.005
    pub fee_rate: f64,           // 0.01
}
```

### Integration with ml_bridge.py

Update env vars when launching the bridge:

```bash
MIN_CONFIDENCE=0.60 MIN_CONSISTENCY=0.60 SIGNAL_WINDOW=30 \
  python ml_bridge.py --mode tcp --port 9999
```

## Key Takeaways

1. **Quality > Quantity** — higher thresholds mean fewer trades but better hit rate
2. **30s min entry is critical** — predictions at second 0-29 are random noise because the ML needs warmup data
3. **Consistency filter removes "flip-flopping" signals** — if the model keeps changing its mind, the signal is weak
4. **This is still fundamentally the 1s ML approach** — v6's drift estimator replaces this pipeline entirely
