# V3: Ensemble Backtest (First Backtester)

## Overview

The first **full backtesting framework**. Takes v2's tick-level ML and wraps it in a proper backtester that simulates trading on Polymarket's 15-minute binary options. Introduces the **stacking ensemble** (XGBoost + LightGBM + LogisticRegression meta-learner) and tests both Hold-to-Resolve and Momentum (take-profit) strategies.

**File:** `backtest_v1.py`
**Status:** Superseded by v4's Rust config mirroring

## Architecture

```
                    Binance Trades (raw)
                         |
                    agg_binance(1s) + agg_binance(5s)
                         |
                    build_features() — 80+ features
                         |
                    train_ensemble()
                    ┌─────────────────────┐
                    │  XGBoost  LightGBM  │  → predict_proba()
                    │         ↓           │  
                    │  LogisticRegression  │  → stacking meta-learner
                    └─────────────────────┘
                         |
               predict_ensemble() → P(UP) for each 1s bar
                         |
               build_market_signals() — aggregate over 5-min window
                         |
              ┌──────────────────────────┐
              │   SIGNAL PER MARKET      │
              │   direction, confidence  │
              │   entry_up_ask, etc.     │
              └──────────────────────────┘
                         |
              ┌──────────┴──────────┐
              │                     │
      Hold-to-Resolve        Momentum (TP sweep)
      (multiple conf)       (multiple TP levels)
```

## Signal Method

1. **Tick aggregation:** Same as v2 (1s + 5s bars from Binance trades)
2. **Feature engineering:** Same 80+ features as v2
3. **Ensemble prediction:** For each 1s bar, get P(UP):
   - XGBoost → P(UP)_xgb
   - LightGBM → P(UP)_lgb
   - Meta LogisticRegression on [P(UP)_lgb, P(UP)_xgb] → final P(UP)
4. **Market-level signal:** For each 15-min market:
   - Collect all 1s predictions in the first 5 minutes (300s)
   - Take mean P(UP) across all predictions
   - If mean > 0.5 → signal UP, confidence = mean
   - If mean <= 0.5 → signal DOWN, confidence = 1 - mean

## Key Config

```python
INITIAL_BANKROLL = 100.0
BET_FRACTION     = 0.05        # 5% per trade
SLIPPAGE         = 0.005       # $0.005 per share
FEE_RATE         = 0.01        # 1% per leg
MIN_CONFIDENCE   = 0.55        # Sweep: 0.55 to 0.95
MOMENTUM_TARGETS = [0.05, 0.10, 0.15, 0.20]
```

## Strategies

### Hold-to-Resolve
- Buy UP or DOWN contract at ASK + slippage
- Hold until market resolves (15 min)
- Correct → payout $1.00/share, Wrong → $0.00/share
- Swept across confidence thresholds 0.55 to 0.95

### Momentum (Take-Profit)
- Buy contract, set take-profit target (5%, 10%, 15%, 20%)
- If TP hit: sell at BID - slippage
- If TP not hit: hold to resolution
- Uses actual Polymarket bid trajectory for realistic TP simulation

## How to Run

```bash
python backtest_v1.py
```

**Outputs:**
- `backtest_results.html` — Interactive equity curve chart with trade log
- Console report with per-strategy P&L, win rate, max drawdown

## Relationship to Rust Engine

**Loose mapping.** This backtester does not mirror the Rust config exactly:
- No drawdown circuit breaker
- No max entry price cap ($0.99)
- No consistency filter
- Uses simple mean of ALL predictions (not rolling window)
- Entry at market open (no MIN_SECS_INTO_MARKET delay)

The Rust engine was v4's target for config mirroring.

## Key Takeaways

1. **The ensemble improves over single XGBoost** — meta-learner smooths out disagreements between XGB and LGB
2. **Simple mean aggregation is noisy** — averaging ALL 300 predictions dilutes the signal
3. **No circuit breaker = unbounded losses** — a bad streak can wipe out the bankroll
4. **Momentum TP=10% outperforms hold-to-resolve in some regimes** — early profit-taking avoids reversal losses
