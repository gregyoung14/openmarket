# An Open-Source High-Frequency Data Pipeline and Machine Learning Research Framework for Polymarket Prediction Markets

> **LaTeX build:** `paper/scripts/compile.sh` → `paper/main.pdf`.
> **Empirical stats:** `paper/scripts/paper/analyze_unified.py` → `assets/stats/characterization.tex`
> **arXiv bundle:** `paper/scripts/export-arxiv.sh` → `openmarket-paper-arxiv-*.tar.gz`.

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
reproducibility commands, model release conventions, and a staged dataset
release. The public Hugging Face archive ships `v0.1-sample`, `v0.2-full`
(202 snapshots), and `v0.4.2-unified` (727M deduped rows), plus a sample
`features/` split and a published `v0.2/` binary-outcome model trained on
unified Parquet step3 features. Active data collection ended 2026-07-01; source
tag `v0.5.0` freezes the research record. OpenMarket enables
research into prediction-market microstructure, forecasting, and execution
rather than claiming persistent trading alpha.

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

Prediction markets aggregate information through prices of contracts tied to
future outcomes [Wolfers and Zitzewitz, 2004; Hanson, 2003]. Polymarket lists
binary outcome markets where contracts settle to 1 if an event occurs and 0
otherwise. BTC 15-minute markets ask whether Bitcoin will be above or below a
reference price at the end of a short window. These markets combine elements of
options, sports-style binary markets, and crypto exchange microstructure.
Recent empirical work documents Polymarket microstructure, arbitrage, and
decentralized prediction-market (DePM) design trade-offs [Dubach, 2026; Saguillo
et al., 2025; Rahman et al., 2025]. Reproducible public infrastructure for
high-frequency Polymarket research remains scarce.

Binance BTC/USDT is used as the external reference stream because it is liquid,
high frequency, and closely related to the Polymarket BTC outcome. The research
problem follows multi-venue price-discovery practice [Hasbrouck, 1995]: align the
external price stream with Polymarket order book updates, derive features,
generate labels, and evaluate models or strategies without leaking future
information. High-frequency order-book dynamics motivate millisecond-resolution
storage and pairing [O'Hara, 2015; Cont et al., 2014].

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
- Public dataset releases on Hugging Face (`v0.1-sample` at repo root, `full/`,
  `unified/`, and a sample `features/` split) plus published model artifacts on
  Hugging Face Models (`v0.2/` recommended, `v0.1/` historical)
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
  API (`ml_export` for legacy SQLite feature CSVs)
- `step3-parquet-export`: step3 binary-calibration features from unified Parquet
- `binary-outcome-trainer`: walk-forward logistic regression + Platt scaling
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
and sensitivity to the alignment window [Dubach, 2026]. OpenMarket treats these
as first-class dataset quality metrics rather than hidden implementation details.

A LaTeX version of this section with a lead-lag timeline figure specification
is provided in `paper/sections/synchronization.tex` (TikZ Figure 1: source vs.
ingest timestamps, alignment window `W`, and `lead_lag_ms = t_P - t_B`).

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

The project includes historical Python prototypes (XGBoost, LightGBM, SHAP,
stacked ensembles in `research/legacy-ml/`) and a **Rust training path** for the
published binary-outcome model:

```text
unified/ Parquet  →  export_step3_from_parquet  →  step3 CSV
                 →  train_binary_outcome_model   →  HF model artifact
```

**Feature export (`step3-parquet-export`).** Reads `market_meta`, Binance trades,
and Polymarket ticks from `v0.4.2-unified` Parquet, emits step3 binary
calibration rows (43 features per snapshot). On the publication workstation this
export completes in ~78s for 354,684 rows across 2,234 markets (50% of 4,450
`market_meta` entries; remaining markets lack sufficient ticks or trades).

**Training (`binary-outcome-trainer`).** Walk-forward logistic regression by
market (555 windows, expanding train horizon), Platt scaling, and simulated +EV
evaluation under stated fee (1%) and slippage (0.5%) assumptions. Training on
354k rows completes in ~67s (Rayon-parallel on Apple M5 Max).

**Published model (`v0.2/` on Hugging Face Models):**

| Metric | Value |
|---|---:|
| AUC-ROC (calibrated OOS) | 0.840 |
| Brier | 0.164 |
| ECE | 0.025 |
| Simulated +EV trades | 260,622 |
| Sim PnL / trade | -0.123 |

The negative simulated PnL is intentional transparency: the artifact demonstrates
calibration and ranking skill, not deployable trading alpha. An earlier `v0.1/`
pilot model (smaller training set) remains for comparison.

Model binaries live on Hugging Face Models; Git ships trainers, feature schemas,
and `scripts/ml/README.md` reproduction commands.

## 9. Strategy Framework

The strategy modules document a research archive (v1–v15 iterations), not a
single validated production system. Strategies combine model confidence, market
price, entry constraints, and risk controls. The current research line includes:

- drift and order-flow imbalance signals
- scoreboard-derived Polymarket book signals
- whipsaw/chop detection
- best-signal scanning inside an entry window
- volume gating
- entry price bounds
- confidence and edge thresholds
- Brier-score circuit breakers
- confidence-bin empirical edge gates

Position sizing and simulated execution assumptions are intentionally separated
from directional accuracy. Backtests should report slippage, fees, assumed fill
prices, and market impact assumptions explicitly; reported outcomes are
counterfactual simulations, not live trading results.

## 10. Backtesting

Backtesting processes market windows independently and evaluates entry signals
against settled outcomes under documented simulation assumptions. Preferred
validation methods include:

- walk-forward validation
- rolling-window evaluation
- strict out-of-sample splits
- sensitivity analysis over entry windows and thresholds
- Monte Carlo resampling of fill and slippage assumptions

Random row splits are discouraged because adjacent high-frequency observations
are highly autocorrelated.

## 11. Evaluation

Evaluation should include both predictive and simulated-economic metrics.
Economic metrics are counterfactual diagnostics under explicit fill, fee, and
slippage assumptions; they do not constitute claims of live or persistent
trading profitability.

Predictive metrics:

- accuracy, precision, recall, F1, ROC AUC
- Brier score and calibration curves [Brier, 1950]

Simulated-economic metrics (require stated fill model):

- simulated win rate, expectancy, PnL
- Sharpe and Sortino ratios under the simulation assumptions
- maximum drawdown, turnover, average entry price
- documented fill, fee, and slippage assumptions

Calibration is especially important because binary market strategies are
sensitive to the difference between predicted probability and market-implied
price.

## 12. Performance

Rust enables high-throughput collection and backtesting. Benchmark categories and
harnesses are documented in the repository; published tables should report:

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

The public dataset lives at:

```text
huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
```

### 13.1 Release Status

| Split | Version | Status | Purpose |
|-------|---------|--------|---------|
| Unified | `v0.4.2-unified` | **Live** | Deduped research timeline — **727M rows**, 8.7 GiB (recommended) |
| Full | `v0.2-full` | **Live** | Complete 202-snapshot per-export archive (3,312 parquet files) |
| Features | `v0.4-features` | **Optional** | One-snapshot demo on HF; full step2/step3 reproducible from `unified/` |
| Sample | `v0.1-sample` | **Live** | 12 flat parquet at repo root; quickstart and CI |

**Archive inventory:** 202 CDN SQLite snapshots (46 GB compressed), collected
2026-03-14 through 2026-07-01. Five formerly-partial snapshots were recovered
via `sqlite3 .recover` and re-exported before the final unified rebuild; queue
metadata reports `202 published-clean`, `0 partial`, `0 corrupt`.

**Unified dedupe:** 916M input rows across overlapping `full/` exports → 727M
output rows (~21% duplicates removed). Produced by
`scripts/datasets/merge_partitions.py`.

All splits are validated via `scripts/hf/validate_sample_split.py`. Empirical
statistics in Section 16 / `experimental-results` are regenerated from on-disk
Parquet via `paper/scripts/paper/analyze_unified.py`.

### 13.2 Target Layout

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

OpenMarket treats reproducibility as a first-class systems requirement. Any
benchmark, backtest, or paper result should be reconstructible from six
identifiers:

- source commit (Git SHA)
- dataset version (HF revision or snapshot manifest hash)
- model version (when applicable; may be `none` for strategy-only runs)
- config file path and contents
- exact command line
- random seed (when stochastic components are used)

### 14.1 Build Verification

After cloning the repository, verify the Rust workspace compiles:

```bash
git clone https://github.com/gregyoung14/openmarket.git
cd openmarket
cargo check --workspace
```

### 14.2 Fast Path (Hugging Face Sample Split)

The recommended public entry point is the published HF sample split
(`v0.1-sample`): 12 tables, 9,352 rows, ~204 KB Parquet, downloadable in
seconds. This path validates dataset layout and schema without a multi-gigabyte
download.

```bash
git clone https://github.com/gregyoung14/openmarket.git
cd openmarket
python3 -m venv .venv
.venv/bin/pip install -r scripts/datasets/requirements.txt -r scripts/hf/requirements.txt
.venv/bin/python scripts/hf/validate_sample_split.py      # round-trip + row-count check
.venv/bin/python scripts/hf/benchmark_baseline.py         # record load-time baseline
```

Optional notebook walkthrough:

```bash
.venv/bin/pip install jupyter pandas matplotlib
.venv/bin/jupyter nbconvert --to notebook --execute notebooks/quickstart.ipynb
```

Strategy backtests that require SQLite can use a converted database from the
full operator archive; the HF sample split alone is intended for schema
validation and baseline benchmarking, not full strategy reproduction.

### 14.3 Full Reproduction (Unified HF Split)

Full strategy reproduction uses the unified Hugging Face Parquet split:

```bash
.venv/bin/python datasets/download.py --split unified --out data/hf_cache
```

### 14.3.1 ML Model Reproduction (Rust)

```bash
cargo build -p step3-parquet-export -p binary-outcome-trainer --release
./target/release/export_step3_from_parquet \
  --parquet-root data/hf_release/unified_parquet \
  --out-dir data/hf_release/features_exports
./target/release/train_binary_outcome_model \
  --input data/hf_release/features_exports/step3_binary_calibration_<ts>.csv \
  --artifact-dir data/ml_artifacts
.venv/bin/python scripts/hf/upload_models.py --version v0.2
```

Legacy operator SQLite snapshots remain available for migration only:

```bash
python3 datasets/download.py --legacy-cdn sample --out data/openmarket.db
cargo run -p v15_brier_calibration --release -- --db-path data/openmarket.db
```

### 14.4 Docker Reproduction

For end-to-end service reproduction (collector, recorder, dashboards):

```bash
cp configs/openmarket.example.env .env
docker compose -f docker/docker-compose.yml up
```

### 14.5 Required Reporting Metadata

Each published result should report:

- CPU model
- RAM
- storage type (SSD/NVMe vs. network)
- OS and kernel version
- Rust toolchain version (`rustc --version`)
- Python version (when scripts are used)
- dataset version or HF revision
- model version (or `none`)
- config file
- command
- random seed

Without this metadata, throughput and backtest numbers cannot be compared across
environments.

### 14.6 Reproducibility Flow

```text
git clone → cargo check → HF sample validate → (optional) notebook
                |
                +→ SQLite snapshot download → strategy backtest
                |
                +→ docker compose up → live collector stack
```

See `docs/reproducibility.md` for the canonical reproduction guide.

## 15. Limitations

OpenMarket has several limitations:

- WebSocket outages can create data gaps.
- Collector host clocks can drift.
- Top-of-book backtests may overestimate execution quality.
- Label definitions can leak information if settlement is mishandled.
- Simulated strategy outcomes may be sensitive to a small number of market regimes.
- Historical Polymarket liquidity may not match future liquidity.
- Live execution has additional latency, queue position, and partial-fill risks.
- BTC 15-minute markets are only one prediction-market domain.
- Step3 feature export skips ~50% of `market_meta` markets (missing ticks or
  insufficient Binance trades).
- Full-archive `features/` Parquet is not published on Hugging Face; researchers
  should use `export_step3_from_parquet` on `unified/` instead.

## 16. Empirical Characterization

Regenerate stats: `paper/scripts/paper/analyze_unified.py` →
`paper/assets/stats/characterization.tex`.

**Scale (unified split, v0.4.2-unified):**

| Table | Rows |
|---|---:|
| `polymarket_ticks_ms` | 605,599,870 |
| `binance_trades` | 62,066,729 |
| `binance_ticks_ms` | 55,791,665 |
| `lag_pairs_ms` | 2,931,191 |
| `market_meta` | 4,450 |
| **Total (11 tables)** | **726,892,430** |

On-disk size: 8.7 GiB. Collection span: 109 days (202 snapshots).

**Lead–lag (`lag_pairs_ms`):** median 16 ms; 5th/95th percentiles -185 / +315 ms.

These statistics describe archival corpus content, not trading profitability.

## 17. Archive Closeout and Research Extensions

OpenMarket is no longer an active data-collection project. Archival closeout
completed on 2026-07-01:

- all 202 CDN manifest snapshots published in `full/` (`202 clean`, `0 partial`)
- five formerly-partial snapshots recovered via `sqlite3 .recover` and re-exported
- `unified/` rebuilt (`v0.4.2-unified`, 727M rows)
- `v0.2/` binary-outcome model published on Hugging Face Models
- queue metadata reconciled; source tag `v0.5.0` freezes the public record

Optional research extensions, if anyone in the open-source community chooses to
continue from this base, include:

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

## 18. Open Source Release

The public release includes:

- GitHub repository: `github.com/gregyoung14/openmarket`
- Hugging Face dataset: `gregyoung14/openmarket-btc-polymarket`
- Hugging Face models: `gregyoung14/openmarket-models` (`v0.2/` walk-forward
  logistic on unified step3; `v0.1/` historical)
- mdBook documentation
- Rust API documentation
- Docker reproducibility
- benchmark tables
- contribution guide
- GitHub issues labeled by contribution area

## 19. References

Bibliography source: `paper/bibliography.bib` (BibTeX). Key references:

- Berg, J. E., Nelson, F. D., and Rietz, T. A. (2008). Prediction Markets as a Research Tool. *The Economists' Voice*, 5(1).
- Brier, G. W. (1950). Verification of Forecasts Expressed in Terms of Probability. *Monthly Weather Review*, 78(1), 1–3.
- Cont, R., Kukanov, A., and Stoikov, S. (2014). The Price Impact of Order Book Events. *Journal of Financial Econometrics*, 12(1), 47–88.
- Dubach, P. D. (2026). The Anatomy of a Decentralized Prediction Market: Microstructure Evidence from the Polymarket Order Book. *arXiv:2604.24366*.
- Hanson, R. (2003). Combinatorial Information Market Design. *Information Systems Frontiers*, 5(1), 107–119.
- Hasbrouck, J. (1995). One Security, Many Markets: Determining the Contributions to Price Discovery. *Journal of Finance*, 50(4), 1175–1199.
- O'Hara, M. (2015). High Frequency Market Microstructure. *Journal of Financial Economics*, 116(2), 257–270.
- Rahman, N., Al-Chami, J., and Clark, J. (2025). SoK: Market Microstructure for Decentralized Prediction Markets (DePMs). *arXiv:2510.15612*.
- Saguillo, O., Ghafouri, V., Kiffer, L., and Suarez-Tangil, G. (2025). Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets. *arXiv:2508.03474*.
- Wolfers, J., and Zitzewitz, E. (2004). Prediction Markets. *Journal of Economic Perspectives*, 18(2), 107–126.

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

## Appendix B. Figure Specifications

Each figure below is specified for LaTeX/TikZ or matplotlib generation. Asset
paths are placeholders under `paper/assets/figures/`.

### B.1 Architecture Diagram (`fig:architecture`)

**Type:** Native TikZ in `sections/architecture.tex`.
**Status:** `[done]`

### B.2 WebSocket Message Flow (`fig:ws-flow`)

**Type:** Sequence diagram.
**Content:** Subscribe → heartbeat → trade/book event → normalize → persist;
show reconnect branch with gap marker.
**Data source:** `docs/architecture/overview.md`.
**Status:** `[TODO]`

### B.3 Synchronization Timeline (`fig:lead-lag-timeline`)

**Type:** Native TikZ in `sections/synchronization.tex`.
**Status:** `[done]`

### B.4 Lead-Lag Histogram (`fig:lead-lag-hist`)

**Type:** Histogram (matplotlib).
**Content:** Distribution of `lead_lag_ms` from `lag_pairs_ms`, faceted by date
and UP/DOWN side. Overlay median and 5th/95th percentiles.
**Data source:** HF `sample/lag_pairs_ms/` or SQLite export.
**Status:** `[TODO: notebook script]`

### B.5 Order Book Snapshot (`fig:orderbook`)

**Type:** Depth chart or bid-ask ladder.
**Content:** Top-of-book bid/ask for UP and DOWN tokens at a single
`polymarket_ticks_ms` timestamp; annotate spread and microprice.
**Status:** `[TODO]`

### B.6–B.10 Pipeline Figures (`fig:features`, `fig:training`, `fig:backtest`,
`fig:schema`, `fig:repro`)

**Type:** Native TikZ in respective `sections/*.tex` files.
**Status:** `[done]`

### B.11 Benchmark Charts (`fig:benchmarks`)

**Type:** Bar or time-series charts.
**Content:** Outputs from `scripts/hf/benchmark_baseline.py` and planned Rust
harnesses (throughput, latency). Label as environment-specific measurements.
**Status:** `[TODO: run harness, plot results]`
