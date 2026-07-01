# V2: High-Frequency ML Bench

## Overview

Upgraded from v1's candle-level to **tick-level prediction** using raw Binance trades. Aggregates trades into 1s/5s/15s/30s bars, computes ~80 microstructure and order flow features, and trains a single XGBoost model to predict the next bar's direction. This is where we discovered that sub-second timeframes show strong signal.

**Files:**
- `high_freq_ml.py` — Main pipeline (multi-timeframe test)
- `squeeze_1s.py` — Focused squeeze on 1s timeframe optimization
- `test_sub_second.py` — 250ms and 500ms timeframe experiments

**Status:** Superseded by v3 as a backtester, but still useful as a feature importance reference

## Signal Method

- **Data source:** `binance_trades` (raw trade stream) + `polymarket_ticks_ms` + `lag_pairs_ms`
- **Aggregation:** Trades bucketed into 1s, 5s, 15s, 30s bars
- **Feature categories (80+ features):**

### Binance Microstructure
| Feature | Description |
|---------|-------------|
| `ret` | 1-bar return |
| `hl` | High-low range / close |
| `co` | Close-open / open |
| `vwap_d` | Close vs VWAP divergence |
| `ivol` | Intra-bar volatility (price std / close) |
| `v3`, `v10`, `vratio` | Rolling vol (3, 10 bars) and vol ratio |

### Order Flow
| Feature | Description |
|---------|-------------|
| `ofi` | (buy_vol - sell_vol) / total_vol |
| `br` | Buy ratio (buy_vol / total_vol) |
| `ofi_m{3,5,10}` | Rolling mean OFI |
| `ofi_a{3,5,10}` | OFI anomaly (OFI - rolling mean) |
| `cum_ofi` | Cumulative OFI over 30 bars |

### Trade Microstructure
| Feature | Description |
|---------|-------------|
| `tc`, `tc_r`, `rtc` | Trade count, change, relative to MA |
| `ats`, `rats` | Avg trade size, relative to MA |
| `whale` | Max trade size / MA(max, 10) — whale detector |

### Technical
| Feature | Description |
|---------|-------------|
| `roc{3,5,10}` | Rate of change |
| `rsi` | 10-period RSI |
| `ema_x` | EMA(5)/EMA(15) cross ratio |
| `rl{1-5}`, `ol{1-5}` | Return and OFI lags |

### Polymarket Cross
| Feature | Description |
|---------|-------------|
| `pup`, `pm3`, `pm5` | Poly UP price and momentum |
| `psp_u`, `psp_d` | Poly spread (ask - bid) |
| `pvr` | Poly volume ratio (UP / total) |
| `pdiv` | Poly/BTC divergence |

### Lead-Lag
| Feature | Description |
|---------|-------------|
| `lgm`, `lgdir`, `lgchg` | Lag mean, direction, change |

- **Model:** Single XGBoost (2000 trees, depth=4, lr=0.01, scale_pos_weight)
- **Target:** Binary — next bar close > current close?
- **Split:** 70% train / 15% val (early stopping) / 15% test

## Best Results

| Timeframe | Overall Acc | High-Conf Acc | Notes |
|-----------|-------------|---------------|-------|
| 250ms | 73.0% | - | Sub-second sweet spot |
| 500ms | 70.4% | - | Still very strong |
| 1s | 62.4% | 77.2% (>65% conf) | Core signal for v3+ |
| 5s | 56.8% | - | Okay but noisier |

## How to Run

```bash
# Full multi-timeframe benchmark
python high_freq_ml.py

# 1s squeeze (optimization)
python squeeze_1s.py

# Sub-second tests
python test_sub_second.py
```

## Relationship to Rust Engine

**Indirect.** The feature engineering pipeline from this script (`build_features()`, `agg_binance()`, etc.) was directly adopted into:
- `live_trader.py` → `FeatureEngine` class (computes features in real-time)
- `ml_bridge.py` → Bridges the Python ML to the Rust execution engine

The model trained here is the same architecture used in v3/v4/v5's stacking ensemble (the XGBoost layer).

## Key Takeaways

1. **Order flow features dominate** — OFI, cumulative OFI, whale detection, and buy ratio are the most important features (confirmed via SHAP in `ensemble_shap.py`)
2. **1s is the production timeframe** — fast enough for signal quality, slow enough not to overfit noise
3. **Sub-second shows the theoretically "true" edge** — at 250ms, we see 73% accuracy, but it's impractical for the 15-min Polymarket resolution window
4. **The high-confidence subset >> overall accuracy** — filtering to >65% confidence yields 77.2% accuracy vs 62.4% overall
