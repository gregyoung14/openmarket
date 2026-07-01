# V1: ML Test Bench

## Overview

The original proof-of-concept. Tests three ML models on Binance 15m/1m candle data (OHLCV) to predict the next candle's direction (UP/DOWN). This was the "can we predict BTC at all?" experiment.

**File:** `ml_test_bench.py`
**Status:** Deprecated — superseded by v2's tick-level approach

## Signal Method

- **Data source:** `binance_candles_15m` or `binance_candles_1m` from SQLite DB
- **Features:** Traditional TA indicators on OHLCV data:
  - SMA(20), EMA(12), RSI(14)
  - MACD line + signal
  - Bollinger Bands (upper/lower)
  - Price change %, volume change %, high-low range
- **Target:** Binary — is next candle's close > current close?
- **Train/test split:** 80/20 chronological

## Models Tested

| Model | Type | Notes |
|-------|------|-------|
| Logistic Regression | `statsmodels.Logit` | Baseline |
| XGBoost | `XGBClassifier` | Default hyperparams |
| PyTorch MLP | 64→32→1 sigmoid | 20 epochs, Adam, BCE loss |

## How to Run

```bash
python ml_test_bench.py
```

Requires: `polymarket_btc_data.db` in the working directory.

## Results

With limited candle data (~38 rows for 15m), models barely learned anything meaningful. This motivated the shift to tick-level data in v2.

## Relationship to Rust Engine

**None.** This was a standalone research script. It does not produce signals compatible with the Rust execution engine. It was purely to validate whether ML can predict BTC direction at all.

## Key Takeaway

OHLCV candle features are too coarse for the 15-minute Polymarket binary option task. The resolution is decided by tick-level price action, not candle-level TA. This led to v2's approach of using raw Binance trades.
