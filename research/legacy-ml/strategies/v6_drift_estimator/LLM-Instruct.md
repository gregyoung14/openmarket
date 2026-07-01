# V6: Drift Estimator (Current / Latest)

## Overview

**Complete paradigm shift.** Replaces the ML ensemble pipeline (v2-v5) with a direct **Brownian drift estimator** combined with price-vs-open scoreboard, cumulative order flow imbalance, and EMA regime. No ML model training — uses closed-form statistical signal with a 30-second confirmation window for noise filtering.

**File:** `backtest_drift.py`
**Status:** CURRENT — best performing strategy (67.9% WR, +47.7% ROI, 9.8% MDD)

## Why This Exists

The v3-v5 ML pipeline generates 1s predictions and averages them over a 30-second rolling window. We realized this is a **noisy approximation of a drift estimator**: the ML is essentially learning whether BTC is drifting up or down. Rather than running an expensive ensemble on every tick, we can compute the drift directly from the raw trades using Brownian motion math.

## Signal Method

### 4-Component Weighted Signal

At each second `S` into the market (from 60s to 600s):

#### Component 1: Brownian Drift Estimator (40% weight)
```
log_returns = diff(log(prices))
mu = mean(log_returns) / dt          # Drift per second
sigma = std(log_returns) / sqrt(dt)  # Volatility per sqrt(second)
z = mu * sqrt(remaining_seconds) / sigma
P(UP at close) = Phi(z)             # Normal CDF
```

This projects the observed drift to market close using Brownian motion math.

#### Component 2: Scoreboard (25% weight)
```
price_vs_open = (current_price - open_price) / open_price
scoreboard_signal = sigmoid(price_vs_open * 5000)
```

"Are we currently winning?" — if price has already moved in one direction, that direction is more likely to hold.

#### Component 3: Order Flow Imbalance (20% weight)
```
ofi = (buy_volume - sell_volume) / total_volume
ofi_signal = sigmoid(ofi * 3)
```

Cumulative buy/sell balance from market open.

#### Component 4: EMA Regime (15% weight)
```
ema_fast = EMA(prices, 10)
ema_slow = EMA(prices, 60)
ema_cross = (fast - slow) / slow
ema_signal = sigmoid(ema_cross * 5000)
```

Trend confirmation.

#### Combined Signal
```
combined_prob_up = 0.40 * drift + 0.25 * scoreboard + 0.20 * ofi + 0.15 * ema

Direction = UP if combined > 0.5 else DOWN
Confidence = combined if UP, (1 - combined) if DOWN
Consistency = fraction of components agreeing on direction (0, 0.25, 0.50, 0.75, 1.00)
```

### 30-Second Confirmation Window

**Critical noise filter.** The signal must point in the **same direction for 30 consecutive seconds** before it fires. This prevents:
- Momentary spikes from triggering false entries
- The drift estimator fitting to noise at short horizons
- Direction flip-flopping in choppy markets

```python
# Scan each second:
if direction == confirm_direction:
    confirm_count += 1
else:
    confirm_direction = direction
    confirm_count = 1  # Reset

if confirm_count >= 30:
    # CONFIRMED — fire signal
```

## Key Config

```python
INITIAL_BANKROLL     = 100.0
BET_FRACTION         = 0.05     # 5% per trade
SLIPPAGE             = 0.005    # $0.005 per share
FEE_RATE             = 0.01     # 1% per leg
MIN_SECS_INTO_MARKET = 60       # Wait 60s before scanning
MAX_SECS_INTO_MARKET = 600      # Max entry at 10 min
MARKET_DURATION_SECS = 900      # 15 min markets
CONFIRMATION_WINDOW  = 30       # 30s stable signal required
MAX_DAILY_LOSS_PCT   = 0.20     # 20% drawdown halt
MAX_ENTRY_PRICE      = 0.99     # Cap
CONFIDENCE_LEVELS    = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
```

## Best Results (on 38 markets, ~36 hours data)

| Strategy | Trades | WR | ROI | Final | MDD |
|----------|--------|----|-----|-------|-----|
| **Hold >75%** | 28 | **67.9%** | **+47.7%** | **$147.74** | **9.8%** |
| Hold >80% | 22 | 68.2% | +36.2% | $136.21 | 6.0% |
| Hold >70% | 32 | 62.5% | +32.6% | $132.63 | 14.3% |
| Mom TP=10% >55% | 38 | 65.8% | +33.7% | $133.72 | - |
| Hold >55% | 38 | 60.5% | +30.5% | $130.52 | 14.3% |

**Sweet spot: >75% confidence, Hold-to-Resolve**

## How to Run

```bash
python backtest_drift.py
```

**Outputs:**
- `drift_backtest_results.html` — Equity curves + full trade log
- `drift_trade_log.csv` — Best strategy trade log
- `drift_confidence_sweep.csv` — Confidence sweep results

## Relationship to Rust Engine

### What Changes in the Rust Engine

The drift estimator **replaces the entire ML pipeline**. Instead of:

```
Rust WS → ml_bridge.py → Ensemble → P(UP) → SignalAggregator → Entry
```

You now have:

```
Rust WS → Binance trades buffer → compute_drift_signal() → Entry
```

### Recommended Rust Config for v6

```rust
pub struct TradingConfig {
    // Signal (NEW — replaces ML ensemble)
    pub signal_method: SignalMethod::Drift,
    pub min_confidence: f64,        // 0.75 (sweet spot from backtest)
    pub confirmation_window: u64,   // 30 seconds
    pub drift_weight: f64,          // 0.40
    pub scoreboard_weight: f64,     // 0.25
    pub ofi_weight: f64,            // 0.20
    pub ema_weight: f64,            // 0.15

    // Timing
    pub min_secs_into_market: u64,  // 60
    pub max_secs_into_market: u64,  // 600

    // Execution (same as v5)
    pub max_entry_price: f64,       // 0.99
    pub max_daily_loss_pct: f64,    // 0.20
    pub bet_fraction: f64,          // 0.05
    pub slippage: f64,              // 0.005
    pub fee_rate: f64,              // 0.01
}
```

### Integration Options

**Option A: Pure Rust (recommended)**
Port `compute_drift_signal()` to Rust. It's only basic math (log, mean, std, norm_cdf, sigmoid) — no ML dependencies:

```rust
fn compute_drift_signal(trades: &[Trade], open_price: f64, entry_secs: f64, remaining_secs: f64) -> (Direction, f64) {
    let prices: Vec<f64> = trades.iter().map(|t| t.price).collect();
    let log_returns: Vec<f64> = prices.windows(2).map(|w| (w[1] / w[0]).ln()).collect();

    let dt = entry_secs / log_returns.len() as f64;
    let mu = mean(&log_returns) / dt;
    let sigma = std(&log_returns) / dt.sqrt();

    let z = mu * remaining_secs.sqrt() / sigma;
    let drift_prob_up = norm_cdf(z);  // Use statrs::Normal

    // ... scoreboard, OFI, EMA, combine, confirm ...
}
```

**Option B: Hybrid (keep ml_bridge.py)**
Modify `ml_bridge.py` to use `compute_drift_signal()` instead of the ensemble:

```python
# In MLBridge.process_message():
# Replace:
#   direction, confidence, raw_prob = self.predictor.predict(features)
# With:
#   direction, confidence, components = compute_drift_signal(...)
```

**Option A is strongly recommended** because the drift estimator is pure math with zero Python/ML dependencies, making it faster and more reliable.

### Data Requirements

The drift estimator only needs **Binance trades** — no Polymarket ticks required for signal generation. Polymarket ticks are still needed for:
- Entry price (best_ask)
- Momentum exit simulation (price trajectory)
- Market slug identification

## Key Takeaways

1. **The drift estimator IS the signal the ML was trying to learn** — except computed directly with no training data required
2. **Confirmation window is essential** — without it, accuracy drops to 50% (coin flip)
3. **~109s average entry time** — enters about 2 minutes in, leaving ~13 minutes of remaining market
4. **No model training = no overfitting risk** — the signal is a closed-form statistical estimator
5. **Pure Rust implementation is trivial** — eliminates the Python ML bridge entirely
6. **More data needed** — 38 markets is a small sample; results must be validated with more data
