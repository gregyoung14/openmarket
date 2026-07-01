# An Open-Source High-Frequency Data Pipeline and Machine Learning Research Framework for Polymarket Prediction Markets

## Abstract

Prediction markets have become increasingly important for forecasting real-world
events, yet publicly available high-frequency datasets and reproducible research
infrastructure remain limited. We present OpenMarket, an open-source Rust
framework for collecting, synchronizing, and analyzing Polymarket order book data
alongside real-time Bitcoin market data from Binance. The framework includes
WebSocket collectors, a millisecond-resolution storage layer, timestamp
synchronization and lead-lag pairing, feature generation, technical indicators,
machine learning research utilities, a strategy framework, and reproducible
backtesting. The primary contribution is not a claim of persistent trading
profitability, but a research platform: source code, dataset schemas,
reproducibility commands, model release conventions, and a dataset release plan
for synchronized Binance/Polymarket market data. OpenMarket is designed to
support academic and industrial research on prediction-market microstructure,
forecasting, and execution.

## 1. Introduction

Prediction markets aggregate information through prices of contracts tied to
future outcomes. Polymarket extends this idea through a crypto-native central
limit order book for binary outcome markets. These markets are useful for
forecasting and microstructure research, but reproducible public infrastructure
is scarce. Most available trading repositories mix private scripts, generated
outputs, model binaries, and undocumented assumptions, making it difficult to
reproduce results or contribute improvements.

OpenMarket addresses this gap by treating prediction-market research as a
systems and data-engineering problem. The objective is to provide an extensible
research platform that enables reproducible experimentation on high-frequency
prediction-market data. The initial domain is BTC 15-minute Polymarket binary
markets paired with Binance BTC/USDT market data, but the architecture is
intended to generalize to additional markets and exchanges.

## 2. Background

Polymarket lists binary outcome markets where contracts settle to 1 if an event
occurs and 0 otherwise. BTC 15-minute markets ask whether Bitcoin will be above
or below a reference price at the end of a short window. These markets combine
elements of options, sports-style binary markets, and crypto exchange
microstructure.

Binance BTC/USDT is used as the external reference stream because it is liquid,
high frequency, and closely related to the Polymarket BTC outcome. The research
problem is to align the external price stream with Polymarket order book updates,
derive features, generate labels, and evaluate models or strategies without
leaking future information.

## 3. Contributions

OpenMarket contributes:

- A Rust implementation of Binance and Polymarket WebSocket collectors
- A market data recorder for millisecond tick storage and market metadata
- A synchronization layer that pairs Binance and Polymarket events and measures
  lead-lag relationships
- A feature-generation path for order book, price, technical, and custom signal
  features
- A backtesting framework for BTC 15-minute binary markets
- A strategy evolution archive covering drift, order-flow imbalance, scoreboard,
  whipsaw, volume gates, calibration, and Brier monitoring
- A legacy ML archive containing Python prototypes for XGBoost, LightGBM, SHAP,
  and stacked classifiers
- A public dataset and model release plan using Hugging Face
- Reproducibility commands, Docker scaffolding, documentation, and benchmarks

## 4. System Architecture

```text
          Binance WS
              |
              v
      Tick Stream Collector
              |
              v
      Timestamp Synchronizer <----- Polymarket WS
              |                          |
              |                          v
              |                  Order Book Collector
              v
       Feature Generator
              |
              v
        ML / Signal Engine
              |
              v
          Backtester
              |
              v
          Evaluation
```

The codebase is organized as a Rust workspace:

- `common`: shared constants and cross-service types
- `exchange-binance`: BTC/USDT trade stream collector and candle persistence
- `exchange-polymarket`: Polymarket CLOB stream collector
- `recorder`: multi-market recorder, normalizer, lag-pairing engine, and export
  API
- `signal-engine`: real-time signal generation
- `execution-engine`: optional paper/live order execution and position tracking
- `paper-executor`: paper execution harness
- `backtester`: historical backtesting and strategy evaluation
- `data-prep`: dataset conversion utilities
- `dataset-downloader`: snapshot download utilities

## 5. Data Collection

### 5.1 Binance

The Binance collector records BTC/USDT trades and derives candle tables at
multiple time resolutions. The source schema includes trade ID, trade timestamp,
price, quantity, quote volume, maker/taker direction, and local receive time.
Millisecond-level tick snapshots preserve both source and ingest timestamps.

### 5.2 Polymarket

The Polymarket collector subscribes to BTC binary market order book updates,
trades, and last-trade-price events. The recorder maps token IDs to market slugs
and side labels using market metadata so that UP and DOWN books can be analyzed
consistently across rolling 15-minute markets.

### 5.3 Storage

The initial recorder stores data in SQLite for operational simplicity. The
public dataset release should export raw and processed tables to partitioned
Parquet. The key recorded tables are:

- `binance_trades`
- `binance_ticks_ms`
- `polymarket_ticks_ms`
- `market_meta`
- `lag_pairs_ms`
- `binance_candles_1s`
- `binance_candles_5s`
- `binance_candles_1m`
- `binance_candles_5m`
- `binance_candles_15m`
- `binance_candles_1h`

## 6. Synchronization

Synchronization is the most important technical component. For each event, the
system distinguishes between source time and ingest time:

```text
source_ts_ms = timestamp emitted by the source or exchange
ingest_ts_ms = timestamp observed by the collector host
```

For paired Binance and Polymarket events, the lead-lag value is:

```text
lead_lag_ms = polymarket_source_ts_ms - binance_source_ts_ms
```

Positive `lead_lag_ms` means the Polymarket event timestamp follows the Binance
event timestamp. Negative values indicate the opposite. Pairing is performed
inside a bounded millisecond window and stored with a quality flag, Binance
price, Polymarket bid, market slug, side label, and price delta in basis points.

Important synchronization risks include clock drift, dropped WebSocket messages,
duplicate payloads, reconnect gaps, stale order book state, out-of-order events,
and sensitivity to the alignment window. OpenMarket treats these as first-class
dataset quality metrics rather than hidden implementation details.

## 7. Feature Engineering

Feature families include:

Order book:

- spread
- best bid and best ask
- microprice
- imbalance
- depth and liquidity
- book update velocity

Price:

- returns
- realized volatility
- momentum
- VWAP deviation
- candle shape

Technical indicators:

- RSI
- EMA
- VWAP
- ATR
- Bollinger Bands
- MACD
- ADX

Custom signals:

- drift score
- order-flow acceleration
- scoreboard signal
- whipsaw/chop detector
- volume gate
- Brier calibration monitor
- confidence-bin empirical edge

## 8. Machine Learning

The project includes historical Python prototypes and Rust signal code. The
legacy ML archive includes XGBoost, LightGBM, logistic meta-classifiers, SHAP
feature analysis, and stacked ensembles. Model inputs are aligned feature
vectors; model outputs are probability estimates for UP or DOWN market
resolution.

For public releases, model binaries should be uploaded to Hugging Face Models or
GitHub Releases. Git should contain only model metadata, feature schemas,
training code, hyperparameters, and reproducibility manifests.

## 9. Strategy Framework

Strategies combine model confidence, market price, entry constraints, and risk
controls. The current strategy research line includes:

- drift and order-flow imbalance signals
- scoreboard-derived Polymarket book signals
- whipsaw/chop detection
- best-signal scanning inside an entry window
- volume gating
- entry price bounds
- confidence and edge thresholds
- Brier-score circuit breakers
- confidence-bin profitability gates

Position sizing and execution realism are intentionally separated from
directional accuracy. Backtests should report slippage, fees, assumed fill
prices, and market impact assumptions.

## 10. Backtesting

Backtesting processes market windows independently and evaluates entry signals
against market outcomes. Preferred validation methods include:

- walk-forward validation
- rolling-window evaluation
- strict out-of-sample splits
- sensitivity analysis over entry windows and thresholds
- Monte Carlo resampling of fill and slippage assumptions

Random row splits are discouraged because adjacent high-frequency observations
are highly autocorrelated.

## 11. Evaluation

Evaluation should include both predictive and economic metrics:

- accuracy
- precision
- recall
- F1
- ROC AUC
- Brier score
- calibration curves
- win rate
- expectancy
- PnL
- Sharpe
- Sortino
- maximum drawdown
- turnover
- average entry price
- fill assumptions

Calibration is especially important because binary market strategies are
sensitive to the difference between predicted probability and market-implied
price.

## 12. Performance

Rust enables high-throughput collection and backtesting. Benchmarks should
report:

- WebSocket messages per second
- normalization latency
- lag-pairing throughput
- feature generation speed
- inference latency
- backtest markets per second
- peak memory
- CPU utilization

Every benchmark should include hardware, OS, Rust version, dataset version,
command, and configuration.

## 13. Dataset

The target public dataset is:

```text
huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
```

Recommended layout:

```text
raw/
  binance_ticks/
  polymarket_books/
processed/
  aligned/
  features/
  labels/
metadata/
  markets/
  schemas/
  checksums/
```

Raw data supports independent research into synchronization and feature
construction. Processed data supports faster model experiments and reproducible
baselines.

## 14. Reproducibility

A result should be reproducible from:

- source commit
- dataset version
- model version
- config file
- command
- random seed

Example:

```bash
git clone https://github.com/gregyoung14/openmarket.git
cd openmarket
python3 datasets/download.py --snapshot sample --out data/openmarket.db
cargo run -p v15_brier_calibration --release -- --db-path data/openmarket.db
```

Docker-based reproduction should also be supported:

```bash
cp configs/openmarket.example.env .env
docker compose -f docker/docker-compose.yml up
```

## 15. Limitations

OpenMarket has several limitations:

- WebSocket outages can create data gaps.
- Collector host clocks can drift.
- Top-of-book backtests may overestimate execution quality.
- Label definitions can leak information if settlement is mishandled.
- Strategy performance may be sensitive to a small number of market regimes.
- Historical Polymarket liquidity may not match future liquidity.
- Live execution has additional latency, queue position, and partial-fill risks.
- BTC 15-minute markets are only one prediction-market domain.

## 16. Future Work

Future work includes:

- Additional exchanges and assets
- Polymarket non-crypto markets
- parquet-native data lake export
- formal clock-drift estimation
- richer order book reconstruction
- neural sequence models
- transformer baselines
- reinforcement learning experiments
- graph neural networks over related markets
- cross-market arbitrage research
- improved execution simulation
- public benchmark leaderboard

## 17. Open Source Release

The public release plan includes:

- GitHub repository: `github.com/gregyoung14/openmarket`
- Hugging Face dataset: `gregyoung14/openmarket-btc-polymarket`
- Hugging Face models: `gregyoung14/openmarket-models`
- mdBook documentation
- Rust API documentation
- Docker reproducibility
- benchmark tables
- contribution guide
- GitHub issues labeled by contribution area

## Appendix A. Schema Summary

### `binance_trades`

Raw BTC/USDT trade stream: trade ID, trade timestamp, price, quantity, quote
volume, taker direction, receive timestamp.

### `binance_ticks_ms`

Millisecond Binance tick snapshots: source timestamp, ingest timestamp, trade
timestamp, price, volume, raw JSON.

### `polymarket_ticks_ms`

Polymarket CLOB events: source timestamp, ingest timestamp, market slug, asset
ID, side label, event type, price, best bid, best ask, size, paired flag, raw
JSON.

### `market_meta`

Market registry: market slug, question, UP and DOWN token IDs, prices, first
seen timestamp, last seen timestamp.

### `lag_pairs_ms`

Matched Binance/Polymarket event pairs: pair timestamp, market, side, tick IDs,
source timestamps, lead-lag, Binance price, Polymarket bid, price delta, quality
flag.

## Appendix B. Figures To Add

- Overall architecture diagram
- WebSocket message flow
- Synchronization timeline
- Lead-lag histogram
- Order book visualization
- Feature engineering pipeline
- Training workflow
- Backtesting engine
- Dataset schema
- Reproducibility flow
- Benchmark charts
