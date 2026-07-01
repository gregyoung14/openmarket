# V9: Regime-Aware Drift Estimator + Time-Aware Gating

## Overview

Addresses the core failure mode identified in live v6/v8 trading: **all four signal components were correlated momentum indicators**, causing the system to work brilliantly in trending markets and collapse in choppy ones.

## Time-Aware Extension (backtest_time_aware_v9.py)

Full backtest across **72 markets** (Feb 12-16) with time-of-day tiered gating.

### Key Finding: Clock-Based Tiers DON'T Help This Dataset

The initial hypothesis (from one live session) was that evening hours (>8PM EST) were bad.
**The data says the opposite:**

| Hour (EST) | Tier | Signals | WR% | Net P&L | Note |
|------------|------|---------|-----|---------|------|
| 6-8h | T3 | 7 | 85.7% | +$25.17 | Pre-market, excellent |
| 9-14h | T1 | 31 | 67.7% | +$77.24 | Core US session, good |
| **15-16h** | **T1** | **9** | **22.2%** | **-$47.05** | **AFTERNOON COLLAPSE** |
| 17h | T1 | 4 | 50.0% | -$1.81 | Mediocre transition |
| 18-19h | T2 | 9 | 88.9% | +$67.09 | Early evening, best! |
| 20-21h | T2/T3 | 5 | 40.0% | -$13.24 | Late evening, poor |

**Result**: Clock-based tiers (Prime/Extended/Thin) **underperform flat betting** because the tier penalties hit the high-accuracy evening hours (18-19h) while allowing full bets during the terrible afternoon hours (15-16h).

### Performance Comparison (all 72 markets)

| Strategy | Trades | WR% | ROI | Final | MDD | PF |
|----------|--------|-----|-----|-------|-----|-----|
| **v9 Flat 5% C>65%** | **64** | **64.1%** | **+107.4%** | **$207.38** | **19.8%** | **1.52** |
| v9 Flat 5% C>80% | 51 | 66.7% | +95.3% | $195.31 | 19.0% | 1.64 |
| v9 Tiered C>65% | 64 | 64.1% | +57.6% | $157.60 | 19.4% | 1.42 |
| v9 Prime-Only C>65% | 30 | 56.7% | +9.8% | $109.76 | 23.4% | 1.12 |
| v9 Skip-Thin C>65% | 53 | 62.3% | +44.2% | $144.25 | 19.4% | 1.37 |

### Actionable Insight for Live Trading

Instead of clock-based tiers, consider **blacklisting specific bad hours** (15-16h EST) or using the **regime gate** (which already filters choppy markets). The 3-4 PM EST window has an anomalous 22% WR — likely corresponding to US market close volatility regime shifts that fool the drift estimator.

### Files

- `backtest_time_aware_v9.py` — Full time-aware backtester
- `time_aware_backtest_results.html` — Interactive chart with all strategies
- `time_aware_trade_log.csv` — Best strategy trade log with hour/tier columns
- `time_aware_sweep.csv` — Full confidence × mode sweep results

v6 went 7-1 in the first 8 trades (trending BTC) then 5-8 in the next 13 (choppy BTC). v8 tried to fix this with EV filtering on price, but the real problem was **directional accuracy**, not contract pricing.

**File:** `backtest_regime_v9.py`
**Status:** EXPERIMENTAL — successor to v6/v8

## Root Cause Analysis (v6/v8 Failures)

### The Problem: 4 Correlated Momentum Echoes

| Component (v6) | What It Actually Measures | Weight |
|----------------|--------------------------|--------|
| Drift (Brownian) | "Has price been going up?" | 40% |
| Scoreboard | "Is price above open?" | 25% |
| OFI (cumulative) | "Are there more buyers?" | 20% |
| EMA Cross | "Is short EMA > long EMA?" | 15% |

**All four say the same thing.** High "consistency" just means BTC moved strongly — it says nothing about whether it will *continue*. In fact, mean-reversion becomes more likely after a strong move.

### Why v8's Fix Failed

v8 added `MAX_ENTRY_PRICE = 0.75` and `MIN_EDGE = 0.05`. This only helps if direction is correct. Trade #26 bought at $0.40 (great value!) but direction was wrong → lost $1.24. The problem was **accuracy**, not pricing.

## V9 Architecture

### 6 Key Changes

#### 1. Regime Gate (NEW — biggest impact)
Classifies the market microstructure before allowing any trade:
```
1-second close prices → path_efficiency + autocorrelation → trend/neutral/chop
```

- **Path efficiency** = |net move| / total distance traveled
  - Random walk (60 bars): ~0.13
  - Trending: > 0.15
  - Choppy: < 0.06
- **Autocorrelation** of 1-second returns: negative = mean-reverting (chop)
- **Rule**: `chop` → skip trade entirely. `neutral` → small confidence penalty.

This would have avoided most of v6's late-session losses (choppy market).

#### 2. Decorrelated Signal Components

| Slot | v6 (correlated) | v9 (decorrelated) | Why |
|------|-----------------|--------------------|----|
| 55% | Drift (40%) | **Drift** (heavier) | Only mathematically grounded component |
| 30% | OFI cumulative (20%) | **OFI Acceleration** | Recent vs earlier pressure (detrended) |
| 15% | Scoreboard (25%) | **Reduced Scoreboard** | sigmoid(x * 1000), was 5000 |
| 0% | EMA (15%) | **Removed** | Lagging indicator, added no info over drift |

**OFI Acceleration** splits the trade window in half and compares recent buying pressure to earlier buying pressure. This measures *change in pressure* rather than cumulative direction — a fundamentally different signal from drift.

#### 3. Reduced Sigmoid Sensitivity
```
v6: sigmoid(price_vs_open * 5000) → $7 BTC move (1 bp) → 0.62 (already strong signal!)
v9: sigmoid(price_vs_open * 1000) → $7 BTC move (1 bp) → 0.525 (mild signal)
```

At 1000x, the scoreboard provides graduated probability rather than snapping to binary 0/1 on any visible move.

#### 4. Adaptive Confirmation Window
```
High volatility → shorter window (15-25s): trend is clear, act fast
Low volatility  → longer window (35-50s): need more data for confidence
```

Based on realized 1-second return standard deviation, normalized against typical BTC 1s vol (~0.0002).

#### 5. True Edge Filter
```python
edge = confidence - (entry_ask + slippage)
if edge < MIN_EDGE:
    skip  # No edge over the market
```

Only trade when our model disagrees with the market price by enough. In live trading (where poly prices are 0.35-0.83), this prevents chasing already-priced moves.

#### 6. Improved Polymarket Lookup

**v6**: Forward-only 10s window → often defaults to 0.50
**v9**: Backward-first search for most recent tick → better price discovery

## Key Config

```python
# Signal architecture
W_DRIFT             = 0.55      # Brownian drift (was 0.40)
W_OFI_ACCEL         = 0.30      # OFI acceleration (replaces 0.20 cumulative)
W_SCOREBOARD        = 0.15      # Reduced scoreboard (was 0.25)
SCOREBOARD_SCALE    = 1000      # Was 5000

# Regime detection
REGIME_TREND_THRESHOLD = 0.15   # path_eff >= for 'trend'
REGIME_CHOP_THRESHOLD  = 0.06   # path_eff < for 'chop'
REGIME_LOOKBACK        = 60     # seconds of data
NEUTRAL_CONF_PENALTY   = 0.02   # confidence penalty for neutral

# Adaptive confirmation
BASE_CONFIRM_WINDOW  = 30       # base (same as v6)
MIN_CONFIRM_WINDOW   = 15       # high vol
MAX_CONFIRM_WINDOW   = 50       # low vol

# Entry filters
MAX_ENTRY_PRICE      = 0.80     # contract cap
MIN_EDGE             = 0.05     # confidence must exceed entry price

# Execution (unchanged)
INITIAL_BANKROLL     = 100.0
BET_FRACTION         = 0.05
SLIPPAGE             = 0.005
FEE_RATE             = 0.01
MIN_SECS_INTO_MARKET = 60
MAX_SECS_INTO_MARKET = 600
MAX_DAILY_LOSS_PCT   = 0.20
```

## Performance Optimization

v9 pre-computes 1-second bars (close, buy_vol, sell_vol) per market before the scanning loop. The inner loop (540 iterations per market) uses numpy array slicing instead of repeated DataFrame filtering, making it significantly faster than v6.

## How to Run

```bash
python strategies/v9_regime_filter/backtest_regime_v9.py
```

**Outputs:**
- `regime_backtest_results.html` — Equity curves + trade log + v9 diagnostics
- `regime_trade_log.csv` — Best strategy trade log (includes regime, path_eff, edge)
- `regime_confidence_sweep.csv` — Confidence × Edge sweep results

## Sweeps

### 1. Confidence sweep (edge=0, no price cap)
Direct comparison to v6 — shows the impact of regime gating + decorrelated signals alone.

### 2. Confidence × Edge sweep (v9 filters active)
2D grid: confidence ∈ {60%, 65%, 70%, 75%} × edge ∈ {0%, 3%, 5%, 8%, 10%}

### 3. Momentum TP sweep
Same as v6, with regime gate active.

## Diagnostics

v9 prints detailed filter statistics:
```
=== V9 Signal Summary ===
Markets scanned:         38
Signals emitted:         28
No-signal markets:       10
  (of which chop-gated): 6    ← 6 markets were too choppy to trade
Edge rejections:         2    ← 2 signals had insufficient edge
Price cap rejections:    0
Entry regime:  trend=22  neutral=6
```

## Rust Integration

### What Changes

The core signal is still pure math (no ML). The Rust implementation needs:

1. **Add regime detection** — path efficiency + autocorrelation on 1s bars
2. **Replace OFI cumulative with OFI acceleration** — split window, compare halves
3. **Remove EMA component** — simplifies the code
4. **Add adaptive confirmation** — vol-based window sizing
5. **Add edge check** — compare confidence to Polymarket ask

```rust
pub struct TradingConfig {
    // Signal (v9)
    pub signal_method: SignalMethod::DriftV9,
    pub min_confidence: f64,          // 0.65-0.75
    pub min_edge: f64,                // 0.05
    pub max_entry_price: f64,         // 0.80
    
    // Regime
    pub regime_trend_threshold: f64,  // 0.15
    pub regime_chop_threshold: f64,   // 0.06
    pub regime_lookback_secs: u64,    // 60
    
    // Weights
    pub drift_weight: f64,            // 0.55
    pub ofi_accel_weight: f64,        // 0.30
    pub scoreboard_weight: f64,       // 0.15
    pub scoreboard_scale: f64,        // 1000.0
    
    // Confirmation
    pub base_confirm_window: u64,     // 30
    pub min_confirm_window: u64,      // 15
    pub max_confirm_window: u64,      // 50
    
    // Execution (unchanged)
    pub bet_fraction: f64,            // 0.05
    pub slippage: f64,                // 0.005
    pub fee_rate: f64,                // 0.01
}
```

### Regime Detection in Rust
```rust
fn detect_regime(close_1s: &[f64]) -> (&'static str, f64, f64) {
    let n = close_1s.len().min(60);
    let recent = &close_1s[close_1s.len()-n..];
    
    // Path efficiency
    let direct = (recent.last().unwrap() - recent.first().unwrap()).abs();
    let total: f64 = recent.windows(2).map(|w| (w[1] - w[0]).abs()).sum();
    let path_eff = direct / (total + 1e-12);
    
    // Return autocorrelation
    let returns: Vec<f64> = recent.windows(2).map(|w| (w[1] / w[0]).ln()).collect();
    let autocorr = lag1_autocorrelation(&returns);
    
    if path_eff >= 0.15 && autocorr > -0.10 { ("trend", path_eff, autocorr) }
    else if path_eff < 0.06 || autocorr < -0.25 { ("chop", path_eff, autocorr) }
    else { ("neutral", path_eff, autocorr) }
}
```
