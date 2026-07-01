# Polymarket BTC 15-Min ML — Strategy Evolution

This directory contains every iteration of the prediction/backtesting strategy, organized chronologically. Each version builds on lessons learned from the previous one.

## Version History

| Version | Name | Signal | Best WR | Best ROI | Status |
|---------|------|--------|---------|----------|--------|
| **v1** | ML Test Bench | OHLCV TA + LogReg/XGB/MLP | ~50% | N/A | Deprecated |
| **v2** | High-Freq ML | Tick-level XGB (80+ feat) | 77.2%* | N/A | Research ref |
| **v3** | Ensemble Backtest | Stacking ensemble + mean agg | ~55% | ~-15% | Superseded |
| **v4** | Rust-Mirror | Ensemble + rolling window | ~58% | ~-10% | Superseded |
| **v5** | Alpha Backtest | Ensemble + strict filters | ~60% | ~+5% | Previous prod |
| **v6** | Drift Estimator | Brownian drift + confirm | 67.9% | +47.7% | Benchmark |
| **v7** | Drift Refinement | 45s confirm, higher w_drift | **77.4%** | +77.4% | High Performance |
| **v8** | **Smart Value** | **EV Filter + Price Cap** | **77.4%** | **Safe ROI** | **PRODUCTION** |

*v2's 77.2% is at sub-second / high-confidence-only — not directly comparable to 15-min market resolution accuracy.

## Evolution Path

```
v1 (OHLCV candles)
 → v2 (tick-level, order flow features)
    → v3 (first backtester, stacking ensemble)
       → v4 (mirrors Rust engine config)
          → v5 (tighter Alpha thresholds)
             → v6 (drift estimator)
                → v7 (refined parameters)
                   → v8 (EV-based execution safety)
```

## Key Insight

The ML ensemble (v2-v5) was essentially learning to detect **Brownian drift** from raw trades. By computing drift directly with `norm.cdf(mu * sqrt(T) / sigma)`, we get cleaner, faster signals with zero training overhead. The 30-second confirmation window replaces the rolling-window signal aggregation — same concept, better execution.

## File Structure

```
strategies/
├── README.md                          ← This file
├── v1_ml_test_bench/
│   ├── ml_test_bench.py               ← OHLCV ML test
│   └── LLM-Instruct.md
├── v2_high_freq_ml/
│   ├── high_freq_ml.py                ← Core tick-level ML
│   ├── squeeze_1s.py                  ← 1s optimization
│   ├── test_sub_second.py             ← 250ms/500ms tests
│   └── LLM-Instruct.md
├── v3_ensemble_backtest/
│   ├── backtest_v1.py                 ← First backtester
│   └── LLM-Instruct.md
├── v4_rust_mirror_backtest/
│   ├── backtest.py                    ← Rust config mirror
│   ├── ml_bridge.py                   ← Python<>Rust bridge
│   ├── live_trader.py                 ← Live trading classes
│   └── LLM-Instruct.md
├── v5_alpha_backtest/
│   ├── backtest_alpha.py              ← Alpha premium
│   ├── ml_bridge.py                   ← Python<>Rust bridge
│   ├── live_trader.py                 ← Live trading classes
│   └── LLM-Instruct.md
└── v6_drift_estimator/                ← CURRENT
    ├── backtest_drift.py              ← Drift backtester
    ├── test_polymarket_ml.py          ← Polymarket-aligned ML test
    └── LLM-Instruct.md
```

## Running Any Version

All scripts expect `polymarket_btc_data.db` in the working directory:

```bash
# From the project root:
python strategies/v6_drift_estimator/backtest_drift.py

# Or copy/symlink the DB:
cd strategies/v6_drift_estimator
ln -s ../../polymarket_btc_data.db .
python backtest_drift.py
```
