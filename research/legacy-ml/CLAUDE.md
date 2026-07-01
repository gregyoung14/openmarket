# Polymarket BTC 15-Min ML Trading System

## LLM Instructions / System Context

> **Purpose**: This document gives any LLM (Claude, GPT, etc.) full context on this repository — what it does, how every piece fits together, and how to deploy it on a VPS alongside existing Rust WebSocket infrastructure.

---

## Repository Overview

This is a **machine learning trading system** that predicts whether Bitcoin's price will go UP or DOWN within each 15-minute Polymarket binary options market, then executes trades on those contracts.

**Core thesis**: Polymarket 15-min BTC markets reprice continuously based on BTC spot movement. By ingesting sub-second trade data from Binance alongside Polymarket's own order book, we can predict the 15-min outcome with **65–68% accuracy** (72% at high confidence), which is massively profitable on binary contracts where correct = $1.00 payout, incorrect = $0.00.

**Backtest result**: Starting with $100, 5% bet sizing → **$164 (+64% ROI)** across 37 markets on a single day, with 67.6% win rate and <20% max drawdown.

---

## File Map

```
polymarket-btc-15min-ML/
│
├── .env.example              # Template for Polymarket API credentials
├── .gitignore                # Excludes .db, models/, .env, logs
├── requirements.txt          # All Python dependencies
├── README.md                 # User-facing project overview
│
├── fetch_db.py               # Downloads the SQLite database from S3
│
├── polymarket_btc_data.db    # [NOT IN GIT] ~1.5GB SQLite database with:
│   ├── binance_trades        # 1.15M raw BTC/USDT trades (trade_time ms, price, qty, is_buyer_maker)
│   ├── polymarket_ticks_ms   # 1.93M Polymarket CLOB ticks (market_slug, side_label, price, bid, ask, size)
│   ├── lag_pairs_ms          # 1.70M Binance↔Polymarket lag measurements (lead_lag_ms, quality_flag)
│   └── market_meta           # 37 market slugs with open prices (btc-updown-15m-{epoch_seconds})
│
├── ---- ML Pipeline (research/training) ----
│
├── ml_test_bench.py          # Initial baseline models (Logistic Regression, XGBoost, MLP)
├── high_freq_ml.py           # Multi-timeframe pipeline (1s, 5s, 15s, 30s) with Polymarket features
├── squeeze_1s.py             # Best single-model: 1s XGBoost with multi-TF context → 64.37% accuracy
├── ensemble_shap.py          # SHAP feature selection + XGB/LightGBM/NN stacking → 65.09% accuracy
├── save_models.py            # Trains final ensemble and saves to ./models/
│
├── ---- Saved Models ----
│
├── models/                   # Serialized trained models (IN GIT)
│   ├── xgb_model.pkl         # XGBoost classifier (~420KB)
│   ├── lgb_model.pkl         # LightGBM classifier (~168KB)
│   ├── meta_clf.pkl          # Logistic regression meta-learner (~1KB)
│   ├── features.json         # Ordered list of 53 feature names
│   └── stats.json            # Feature means/stds, training metadata
│
├── ---- Backtesting ----
│
├── backtest.py               # Comprehensive backtester (hold-to-resolve + momentum strategies)
├── backtest_results.html     # Interactive equity curve dashboard
├── trade_log.csv             # Trade-by-trade log from backtest
│
├── ---- Live Trading ----
│
├── live_trader.py            # Full live trading engine (WebSockets → ML → Execution)
├── live_trader.log           # [NOT IN GIT] Runtime log
└── live_trades.json          # [NOT IN GIT] Closed trade records
```

---

## Data Model

### Database Schema

**`binance_trades`** — Raw BTC/USDT trades from Binance, millisecond precision:
| Column | Type | Description |
|--------|------|-------------|
| `trade_time` | INTEGER | Unix timestamp in milliseconds |
| `price` | REAL | Trade price in USDT |
| `quantity` | REAL | Trade size in BTC |
| `quote_volume` | REAL | Trade size in USDT |
| `is_buyer_maker` | INTEGER | 0 = taker buy (aggressive buy), 1 = taker sell |

**`polymarket_ticks_ms`** — Polymarket CLOB order book updates:
| Column | Type | Description |
|--------|------|-------------|
| `market_slug` | TEXT | e.g. `btc-updown-15m-1770895800` |
| `source_ts_ms` | INTEGER | Timestamp in milliseconds |
| `side_label` | TEXT | `UP` or `DOWN` |
| `event_type` | TEXT | `price_change`, `trade`, etc. |
| `price` | REAL | Last trade price (0.00 – 1.00) |
| `best_bid` | REAL | Best bid price |
| `best_ask` | REAL | Best ask price |
| `size` | REAL | Trade/order size (in USDC shares) |

**`lag_pairs_ms`** — Binance↔Polymarket timing analysis:
| Column | Type | Description |
|--------|------|-------------|
| `paired_at_ms` | INTEGER | When the pair was observed |
| `lead_lag_ms` | REAL | Milliseconds Binance leads Polymarket (negative = Poly leads) |
| `quality_flag` | TEXT | `tight`, `medium`, or `loose` |

**`market_meta`** — Market open/initial prices:
| Column | Type | Description |
|--------|------|-------------|
| `market_slug` | TEXT | Market identifier |
| `up_price` | REAL | Opening UP contract price |
| `down_price` | REAL | Opening DOWN contract price |

### Market Slug Format
`btc-updown-15m-{EPOCH_SECONDS}` — the epoch is the **market start** time in Unix seconds. The market resolves at `EPOCH + 900` seconds (15 minutes). Markets start on exact 15-minute boundaries.

### Market Resolution
- If BTC price at resolve time > BTC price at start → **UP resolves to $1.00**, DOWN to $0.00
- If BTC price at resolve time < BTC price at start → **DOWN resolves to $1.00**, UP to $0.00
- As resolution approaches, contract prices converge: winning side bid→0.99/ask→1.00, losing side→0.00/0.01

---

## ML Architecture

### Feature Engineering (53 features from 3 data sources)

All features are computed on **1-second aggregated bars** with 5-second context.

**Price/Microstructure (from Binance):**
- `ret` — 1s return
- `hl` — High-low range / close
- `co` — Close-open change / open
- `vwap_d` — VWAP deviation (close vs VWAP)
- `ivol` — Intra-bar volatility (price std / close)
- `ofi` — Order Flow Imbalance: `(buy_vol - sell_vol) / total_vol`
- `br` — Buy ratio
- `ofi_m3/5/10` — Rolling OFI mean
- `ofi_a3/5/10` — OFI anomaly (current - rolling mean)
- `cum_ofi` — Cumulative OFI over 30 bars
- `tc_r` — Trade count rate of change
- `rtc` — Relative trade count (vs 5-bar mean)
- `rats` — Relative average trade size
- `whale` — Whale detection (max trade / rolling mean max)
- `v3`, `v10` — Rolling volatility (3-bar, 10-bar)
- `vratio` — Volatility ratio (v3/v10)
- `roc3/5/10` — Rate of change (3, 5, 10 bars)
- `rsi` — Relative Strength Index (10-period)
- `ema_x` — EMA crossover (5 vs 15 period)

**Polymarket (TOP SIGNAL SOURCE):**
- `pup` — Polymarket UP contract last price (crowd probability of BTC going up)
- `psp_u` — UP contract spread (ask - bid) — **#1 most important feature by SHAP**
- `psp_d` — DOWN contract spread
- `pm3`, `pm5` — Polymarket momentum (3, 5 bar price change)
- `pd1` — Polymarket 1-bar price diff
- `pvr` — Polymarket volume ratio (UP vol / total vol)
- `pdiv` — Price divergence (Polymarket change vs Binance return)

**Lead-Lag (from lag_pairs_ms):**
- `lgm` — Mean lead-lag in ms (negative = Polymarket leads)
- `lgpr` — Proportion of positive lags
- `lgdir` — Lag direction sign
- `lgchg` — Lag change

**Temporal:**
- `hour_sin`, `hour_cos` — Cyclic time-of-day encoding

**Context:**
- `cross_tf` — Cross-timeframe divergence (1s return vs 5s return)
- `rl1..5` — Lagged returns (1-5 bars)
- `ol1..5` — Lagged OFI (1-5 bars)
- `pl1..3` — Lagged Polymarket UP price (1-3 bars)

### Model Ensemble

```
                    ┌──────────────┐
     53 features ──►│   XGBoost    │──► P(up)_xgb ──┐
                    │  depth=4     │                 │
                    │  lr=0.01     │                 │  ┌──────────────┐
                    └──────────────┘                 ├─►│  Logistic     │──► Final P(up)
                    ┌──────────────┐                 │  │  Regression   │
     53 features ──►│  LightGBM   │──► P(up)_lgb ──┘  │  Meta-Learner │
                    │  depth=4     │                    └──────────────┘
                    │  lr=0.01     │
                    └──────────────┘
```

- Train/val/test split: **70/15/15** (chronological, no shuffling)
- Scale_pos_weight: ~1.91 (compensates class imbalance)
- XGBoost: 2000 estimators, early stopping at best_iteration=212
- LightGBM: 2000 estimators, early stopping at best_iteration=84
- Meta-learner: LogisticRegression(C=1.0), trained on validation set predictions

### Performance

| Metric | Value |
|--------|-------|
| Test accuracy (all) | 65.09% |
| Accuracy at >60% confidence | 70.6% |
| Accuracy at >65% confidence | 74.7% |
| Accuracy at >70% confidence | 78.1% |
| Market-level signal accuracy | 67.6% (25/37 markets) |
| Backtest ROI (hold-to-resolve) | +64% |

---

## How Inference Works

### Feature Computation (Streaming)

The `FeatureEngine` class in `live_trader.py` maintains:
1. A **rolling buffer** of 120 1-second bars (`deque(maxlen=120)`)
2. A **5-second context buffer** (`deque(maxlen=30)`)
3. Real-time Polymarket state (latest bid/ask/price for UP and DOWN)
4. Lead-lag estimate buffer

Every second, when a new 1s bar completes:
1. Raw trades are aggregated into OHLCV + microstructure metrics
2. The full 53-feature vector is computed using rolling windows over the buffer
3. The ensemble model produces P(UP) ∈ [0, 1]

### Signal Aggregation

Individual 1s predictions are noisy. The `SignalAggregator` averages the last N predictions (default 30) into a market-level signal with:
- **Direction**: UP if mean P(UP) > 0.5, else DOWN
- **Confidence**: The strength of the directional lean
- **Consistency**: What % of individual predictions agree

A trade is only entered when:
- confidence ≥ 0.60
- consistency ≥ 60%
- 30s ≤ time into market ≤ 600s (don't trade too early or too late)

### Model Files

To run inference you need these files from `./models/`:
- `xgb_model.pkl` — Serialized XGBClassifier (joblib format, sklearn/xgboost)
- `lgb_model.pkl` — Serialized LGBMClassifier (joblib format, lightgbm)
- `meta_clf.pkl` — Serialized LogisticRegression (joblib format, sklearn)
- `features.json` — Ordered list of 53 feature names

**Loading in Python:**
```python
import joblib, json, numpy as np

xgb_model = joblib.load('models/xgb_model.pkl')
lgb_model = joblib.load('models/lgb_model.pkl')
meta_clf  = joblib.load('models/meta_clf.pkl')

with open('models/features.json') as f:
    feature_names = json.load(f)

# Predict — X is a numpy array or DataFrame with shape (1, 53)
xgb_prob = xgb_model.predict_proba(X)[:, 1]  # P(UP) from XGBoost
lgb_prob = lgb_model.predict_proba(X)[:, 1]  # P(UP) from LightGBM
meta_input = np.column_stack([lgb_prob, xgb_prob])  # ORDER MATTERS: LGB first, XGB second
final_prob = meta_clf.predict_proba(meta_input)[:, 1][0]  # Final P(UP)
```

---

## VPS Deployment & Rust Integration

### Architecture for VPS

```
┌───────────────────────────────────────────────────────────────────────┐
│  VPS (Linux)                                                         │
│                                                                      │
│  ┌──────────────────┐    JSON/pipe     ┌──────────────────────────┐  │
│  │  RUST WebSocket  │────────────────►│  Python ML Service        │  │
│  │  Ingest Service  │                 │  (live_trader.py or       │  │
│  │                  │                 │   custom FastAPI wrapper)  │  │
│  │  - Binance WS    │  ◄────────────  │                          │  │
│  │  - Polymarket WS │   signal back   │  Loads models from disk  │  │
│  │                  │                 │  Computes features        │  │
│  └──────────────────┘                 │  Runs ensemble inference  │  │
│           │                           └──────────────────────────┘  │
│           │ trade signal                                             │
│           ▼                                                          │
│  ┌──────────────────┐                                                │
│  │  RUST Execution  │                                                │
│  │  Engine          │                                                │
│  │  - Place orders  │                                                │
│  │  - Manage pos    │                                                │
│  │  - Risk controls │                                                │
│  └──────────────────┘                                                │
└───────────────────────────────────────────────────────────────────────┘
```

### Option A: Python process, Rust feeds data via stdin/pipe

The simplest integration. Your Rust WS process writes JSON lines to stdout, Python reads them.

**Rust side** — write to stdout:
```rust
// For each Binance trade:
println!(r#"{{"type":"binance","T":{},"p":"{}","q":"{}","m":{}}}"#,
    trade.trade_time, trade.price, trade.qty, trade.is_buyer_maker);

// For each Polymarket tick:
println!(r#"{{"type":"poly","side":"{}","price":{},"bid":{},"ask":{},"size":{},"ts":{}}}"#,
    tick.side, tick.price, tick.best_bid, tick.best_ask, tick.size, tick.timestamp_ms);
```

**Python side** — read from stdin or a named pipe:
```python
import sys, json
from live_trader import FeatureEngine, EnsemblePredictor, TradingConfig

config = TradingConfig()
predictor = EnsemblePredictor()
feature_engine = FeatureEngine(config, predictor.feature_list)

for line in sys.stdin:
    msg = json.loads(line.strip())

    if msg['type'] == 'binance':
        feature_engine.on_binance_trade(
            trade_time_ms=msg['T'],
            price=float(msg['p']),
            qty=float(msg['q']),
            is_buyer_maker=msg['m'],
        )
    elif msg['type'] == 'poly':
        feature_engine.on_polymarket_tick(
            side=msg['side'],
            price=msg['price'],
            bid=msg['bid'],
            ask=msg['ask'],
            size=msg['size'],
            ts_ms=msg['ts'],
        )

    # Every second, compute features and predict
    features = feature_engine.compute_features()
    if features is not None:
        direction, confidence, raw_prob = predictor.predict(features)
        # Output signal back to Rust via stdout
        signal = json.dumps({
            "direction": direction,
            "confidence": float(confidence),
            "raw_prob": float(raw_prob),
            "timestamp": int(time.time() * 1000),
        })
        print(signal, flush=True)  # Rust reads this
```

**Run**: `./rust_ws_ingest | python3 ml_bridge.py | ./rust_executor`

### Option B: TCP Socket / Unix Domain Socket

Python runs as a persistent service listening on a socket. Rust connects and sends data as JSON frames.

```python
# ml_service.py — TCP socket server
import socket, json, threading
from live_trader import FeatureEngine, EnsemblePredictor, TradingConfig

config = TradingConfig()
predictor = EnsemblePredictor()
engine = FeatureEngine(config, predictor.feature_list)

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(('127.0.0.1', 9999))
server.listen(1)

conn, addr = server.accept()
buffer = b""

while True:
    data = conn.recv(4096)
    buffer += data
    while b'\n' in buffer:
        line, buffer = buffer.split(b'\n', 1)
        msg = json.loads(line)
        # ... same processing as Option A ...
        features = engine.compute_features()
        if features is not None:
            d, c, p = predictor.predict(features)
            conn.sendall(json.dumps({"d": d, "c": float(c)}).encode() + b'\n')
```

### Option C: HTTP API (FastAPI)

Best for clean separation. Python runs as a microservice, Rust calls it.

```python
# ml_api.py
from fastapi import FastAPI
from pydantic import BaseModel
import joblib, json, numpy as np
from live_trader import FeatureEngine, EnsemblePredictor, TradingConfig

app = FastAPI()
config = TradingConfig()
predictor = EnsemblePredictor()
engine = FeatureEngine(config, predictor.feature_list)

class BinanceTrade(BaseModel):
    T: int      # trade_time_ms
    p: float    # price
    q: float    # quantity
    m: bool     # is_buyer_maker

class PolyTick(BaseModel):
    side: str
    price: float
    bid: float
    ask: float
    size: float
    ts: int

@app.post("/binance")
def on_binance(trade: BinanceTrade):
    engine.on_binance_trade(trade.T, trade.p, trade.q, trade.m)
    return {"ok": True}

@app.post("/poly")
def on_poly(tick: PolyTick):
    engine.on_polymarket_tick(tick.side, tick.price, tick.bid, tick.ask, tick.size, tick.ts)
    return {"ok": True}

@app.get("/predict")
def predict():
    features = engine.compute_features()
    if features is None:
        return {"ready": False}
    d, c, p = predictor.predict(features)
    return {"direction": d, "confidence": float(c), "raw_prob": float(p)}

# Run: uvicorn ml_api:app --host 127.0.0.1 --port 8000
# Rust calls: POST /binance, POST /poly, GET /predict
```

### Option D: Shared Memory / Memory-Mapped File

For absolute lowest latency. Python writes the 53-feature vector to a memory-mapped file, Rust reads it and runs inference directly (requires porting model to ONNX or implementing tree traversal in Rust).

---

## VPS Setup Instructions

### 1. Clone and Install

```bash
git clone https://github.com/gregyoung14/polymarket-btc-15min-ML.git
cd polymarket-btc-15min-ML

# Python 3.10+ required
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# If torch gives issues on Linux VPS (no GPU):
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 2. Get the Database (for retraining only)

```bash
python fetch_db.py
# Downloads polymarket_btc_data.db (~1.5GB) from S3
```

### 3. Train/Save Models

```bash
python save_models.py
# Creates ./models/ with xgb_model.pkl, lgb_model.pkl, meta_clf.pkl, features.json
```

### 4. Copy Pre-Trained Models (faster)

If you already trained locally, just scp the models directory:
```bash
scp -r ./models/ user@vps:/path/to/polymarket-btc-15min-ML/models/
```

### 5. Verify Models Load

```bash
python -c "
from live_trader import EnsemblePredictor
p = EnsemblePredictor()
print(f'Models loaded: {len(p.feature_list)} features')
"
```

### 6. Set Up Polymarket Credentials (for live execution)

```bash
cp .env.example .env
# Edit .env with your real API credentials:
# POLY_API_KEY=xxx
# POLY_API_SECRET=xxx
# POLY_PASSPHRASE=xxx
```

### 7. Run Live Trader (standalone mode)

```bash
# Dry run (paper trading, no real orders):
python live_trader.py

# Live mode:
python live_trader.py --live --bankroll 100 --min-conf 0.60 --strategy HOLD_TO_RESOLVE

# With Rust pipe integration:
./rust_ws_ingest | python ml_bridge.py | ./rust_executor
```

---

## Integration Protocol (Rust ↔ Python)

### Data Format: JSON Lines (newline-delimited JSON)

**Rust → Python (data feed):**

```jsonc
// Binance trade
{"type":"binance","T":1770895800123,"p":68150.25,"q":0.003,"m":false}

// Polymarket tick
{"type":"poly","side":"UP","price":0.45,"bid":0.44,"ask":0.46,"size":100.5,"ts":1770895800456,"slug":"btc-updown-15m-1770895800"}
```

**Python → Rust (signals):**

```jsonc
// ML signal (emitted every 1 second after buffer warms up)
{"direction":"DOWN","confidence":0.673,"raw_prob":0.327,"timestamp":1770895830000,"market":"btc-updown-15m-1770895800"}

// Trade entry signal (emitted when signal fires)
{"action":"ENTER","side":"DOWN","confidence":0.71,"entry_ask":0.545,"market":"btc-updown-15m-1770895800","market_end_ms":1770896700000}

// Trade exit signal
{"action":"EXIT","side":"DOWN","exit_type":"TAKE_PROFIT","exit_bid":0.65,"market":"btc-updown-15m-1770895800"}
```

### Key Integration Points for Rust

1. **Market identification**: Parse slug to get epoch: `slug.split('-').last().parse::<u64>()`. Market end = start + 900 seconds.

2. **Timing**: Feed data as fast as it comes. Python handles 1s bucketing internally.

3. **Signal delay**: After WebSocket connects, Python needs ~15 seconds to warm up the feature buffer before first prediction.

4. **Thread safety**: The `FeatureEngine` uses internal locks. Safe to call `on_binance_trade()` and `on_polymarket_tick()` from different threads.

5. **Execution on signal**: When Python emits an `ENTER` action, Rust should:
   - Buy `shares = (bankroll * 0.05 * 0.99) / (entry_ask + 0.005)` shares of the indicated side
   - Track the position until market resolution or take-profit hit
   - On resolution: winning side → $1.00/share payout, losing → $0.00

---

## Retraining Models

If you collect more data (more market windows), retrain:

```bash
# 1. Update the database with new data (your scraper writes to polymarket_btc_data.db)
# 2. Retrain:
python save_models.py
# 3. Models are overwritten in ./models/
# 4. Restart the live trader to pick up new models
```

You can also run the full evaluation pipeline to check performance:
```bash
python squeeze_1s.py        # Best single model (XGBoost)
python ensemble_shap.py     # Full ensemble with SHAP analysis
python backtest.py           # Comprehensive backtest
```

---

## Feature Reference (Quick Lookup)

| # | Name | SHAP Rank | Source | Description |
|---|------|-----------|--------|-------------|
| 1 | `psp_u` | #1 | Polymarket | UP contract bid-ask spread |
| 2 | `vwap_d` | #2 | Binance | VWAP deviation |
| 3 | `psp_d` | #3 | Polymarket | DOWN contract bid-ask spread |
| 4 | `co` | #4 | Binance | Close-open return |
| 5 | `hour_sin` | #5 | Time | Time of day (sin component) |
| 6 | `ofi_a10` | #6 | Binance | OFI anomaly (10-bar) |
| 7 | `hl` | #7 | Binance | High-low range |
| 8 | `pvr` | #8 | Polymarket | UP/DOWN volume ratio |
| 9 | `pup` | #9 | Polymarket | UP last price |
| 10 | `pd1` | #10 | Polymarket | UP 1-bar diff |

The top 3 features are all **Polymarket-derived** — the prediction market's own pricing is the strongest leading indicator.

---

## Important Notes

1. **Binary contracts**: These are NOT leveraged. Buy at $0.50, correct = $1.00 (100% return per trade), wrong = $0.00 (total loss of bet). The edge is in prediction accuracy.

2. **Fees**: Polymarket charges ~1% per leg. Factor this into all P&L calculations.

3. **Slippage**: Model assumes $0.005 slippage per share. In practice, depends on liquidity.

4. **Data requirements**: The model requires **both** Binance trade data AND Polymarket tick data to function. Without Polymarket data, the top features are missing and accuracy drops significantly.

5. **Meta-learner input order**: When calling `meta_clf.predict_proba()`, the input must be `[lgb_prob, xgb_prob]` — **LightGBM first, XGBoost second**. Reversing this will produce wrong results.

6. **`models/features.json`** defines the exact column order for model input. The array is:
   ```
   ["lgm", "lgpr", "hl", "co", "vwap_d", "ivol", "ofi", "br", "ofi_m3", "ofi_a3",
    "ofi_m5", "ofi_a5", "ofi_m10", "ofi_a10", "cum_ofi", "tc_r", "rtc", "rats",
    "whale", "v3", "v10", "vratio", "roc3", "roc5", "roc10", "rsi", "ema_x",
    "pup", "psp_u", "psp_d", "pm3", "pm5", "pd1", "pvr", "pdiv", "lgdir", "lgchg",
    "hour_sin", "hour_cos", "cross_tf", "rl1", "ol1", "rl2", "ol2", "rl3", "ol3",
    "rl4", "ol4", "rl5", "ol5", "pl1", "pl2", "pl3"]
   ```
