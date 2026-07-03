# OpenMarket: The First Public Synchronized Polymarket-Binance Dataset for High-Frequency Prediction Market Research

> **LaTeX build:** `paper/scripts/compile.sh` → `paper/main.pdf`.
> **Empirical stats:** `analyze_unified.py` + `analyze_research.py` → `assets/stats/`
> **arXiv bundle:** `paper/scripts/export-arxiv.sh` → `openmarket-paper-arxiv-*.tar.gz`.

## Abstract

We release OpenMarket, to our knowledge the first public synchronized
high-frequency corpus pairing Polymarket BTC 15-minute binary markets with
Binance BTC/USDT. The release combines a frozen Hugging Face archive with a
reproducible Rust pipeline for collection, millisecond pairing, Parquet export,
and walk-forward calibration. The archive (tag `v0.5.0`) spans 109 days, 727.1M
deduplicated events across 202 snapshots, and 2.94M explicit lead–lag pairs.
Initial analyses establish Polymarket stylized facts (median one-tick spreads),
characterize heavy-tailed cross-venue timing with a compact 16 ms median lag,
and benchmark forecasts against naive mid priors—multivariate logistic models
yield modest AUC gains without tradable simulated edge. We position this work as
a data-and-methods release enabling microstructure and forecasting research, not
a trading-alpha claim.

## 1. Introduction

Polymarket's short-horizon BTC binary markets combine prediction-market
forecasting with crypto-native CLOB microstructure. Empirical work now documents
tick-level Polymarket dynamics, DePM design trade-offs, and combinatorial
arbitrage [Dubach, 2026; Saguillo et al., 2025; Rahman et al., 2025]. What
remains missing is a **public, cross-venue, millisecond-resolution corpus**
paired with Binance BTC/USDT and tooling that makes synchronization quality,
lead–lag, and calibration analysis reproducible at archival scale.

We release OpenMarket to close that gap. The contribution is threefold:

1. **Corpus.** To our knowledge, the first public synchronized Polymarket–Binance
   dataset: 727.1M deduplicated events across 202 snapshots (2026-03-14–2026-07-01),
   including 2.94M explicit lead–lag pairs.
2. **Methods.** Documented source-vs.-ingest synchronization, Parquet-native
   export, walk-forward calibration training, and validation harnesses that treat
   clock drift and pairing-window sensitivity as measurable objects.
3. **Empirical baselines.** Stylized facts and forecast benchmarks on the
   released corpus—top-of-book spreads, lead–lag distributions, and ablations
   showing when multivariate models outperform naive order-book mid priors.

We do **not** claim persistent trading profitability. Simulated economics under
stated fees and slippage are negative for the published scorer. The goal is to
enable the community to study *how* external spot prices and Polymarket books
interact at high frequency. OpenMarket is frozen as a public research archive
(source tag `v0.5.0`); active collection has ended.

## 2. Related Work

Prediction markets aggregate dispersed information [Wolfers and Zitzewitz, 2004;
Hanson, 2003]. High-frequency microstructure methods emphasize order-book
dynamics and latency [O'Hara, 2015; Cont et al., 2014]. Multi-venue studies
measure price discovery contributions [Hasbrouck, 1995].

**Polymarket microstructure.** Dubach [2026] analyzes tick-level Polymarket
order-book evidence but does not ship a synchronized Binance reference stream or
Hugging Face archival corpus. Saguillo et al. [2025] document combinatorial
arbitrage using proprietary-scale scrapes. Rahman et al. [2025] survey DePM
design trade-offs without a cross-venue BTC 15-minute benchmark.

**Open infrastructure.** OpenMarket differs by publishing (i) raw and deduped
Parquet splits, (ii) explicit `lag_pairs_ms` with quality flags, (iii) Rust
exporters/trainers with pinned commands, and (iv) empirical baselines on the
released corpus.

| Artifact | PM ticks | Binance ref. | HF corpus | Cross-venue pairs | OSS repro |
|---|---|---|---|---|---|
| Dubach (2026) | ✓ | — | partial | — | ✓ |
| Saguillo (2025) | ✓ | — | — | — | partial |
| Public Binance dumps | — | ✓ | ✓ | — | ✓ |
| OpenMarket (this work) | ✓ | ✓ | ✓ | ✓ | ✓ |

## 3. Background

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

## 4. Contributions

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
  Hugging Face Models (`v0.2.1/` recommended, `v0.1/` historical)
- Reproducibility commands, Docker scaffolding, documentation, and benchmarks

## 5. System Architecture

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

The codebase is a Rust workspace spanning exchange collectors, a multi-market
recorder with lag-pairing export, signal and execution engines, backtesting, and
Parquet-native ML crates. Crate-level detail is in Appendix B.

## 6. Data Collection

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

## 7. Synchronization

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

## 8. Feature Engineering

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

## 9. Machine Learning

The project includes historical Python prototypes (XGBoost, LightGBM, SHAP,
stacked ensembles in `research/legacy-ml/`) and a **Rust training path** for the
published binary-outcome model:

```text
unified/ Parquet  →  export_step3_from_parquet  →  step3 CSV
                 →  train_binary_outcome_model   →  HF model artifact
```

**Feature export (`step3-parquet-export`).** Reads `market_meta`, Binance trades,
and Polymarket ticks from `v0.4.3-unified` Parquet, emits step3 binary
calibration rows (43 features per snapshot). On the publication workstation this
export completes in ~63s for 357,390 rows across 2,251 markets (51% of 4,450
`market_meta` entries; remaining markets lack sufficient ticks or trades).

**Training (`binary-outcome-trainer`).** Walk-forward logistic regression by
market (559 windows, expanding train horizon), Platt scaling, and simulated +EV
evaluation under stated fee (1%) and slippage (0.5%) assumptions. Training on
357k rows completes in ~67s (Rayon-parallel on Apple M5 Max).

**Published model (`v0.2.1/` on Hugging Face Models):**

| Metric | Value |
|---|---:|
| Pooled walk-forward OOS AUC-ROC | 0.838 |
| Brier | 0.165 |
| ECE | 0.025 |
| Simulated +EV trades | 260,617 |
| Sim PnL / trade | -0.117 |

For baseline comparison, the frozen exported scorer is also evaluated on the
full 357,390-row step3 timeline; that full-timeline score is AUC 0.841, Brier
0.163, ECE 0.027, and is the value used for the naive-prior effect-size test in
Section 18.

The negative simulated PnL is intentional transparency: the artifact demonstrates
calibration and ranking skill, not deployable trading alpha. An earlier `v0.1/`
pilot model (smaller training set) remains for comparison.

Model binaries live on Hugging Face Models; Git ships trainers, feature schemas,
and `scripts/ml/README.md` reproduction commands.

## 10. Strategy Framework

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

## 11. Backtesting

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

## 12. Evaluation

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

## 13. Performance

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

## 14. Dataset

The public dataset lives at:

```text
huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
```

### 13.1 Release Status

| Split | Version | Status | Purpose |
|-------|---------|--------|---------|
| Unified | `v0.4.3-unified` | **Live** | Deduped research timeline — **727M rows**, 8.7 GiB (recommended) |
| Full | `v0.2-full` | **Live** | Complete 202-snapshot per-export archive (3,312 parquet files) |
| Features | `v0.4-features` | **Optional** | One-snapshot demo on HF; full step2/step3 reproducible from `unified/` |
| Sample | `v0.1-sample` | **Live** | 12 flat parquet at repo root; quickstart and CI |

**Archive inventory:** 202 CDN SQLite snapshots (46 GB compressed), collected
2026-03-14 through 2026-07-01. Five formerly-partial snapshots were recovered
via `sqlite3 .recover` and re-exported before the final unified rebuild; queue
metadata reports `202 published-clean`, `0 partial`, `0 corrupt`.

**Unified dedupe:** 916M input rows across overlapping `full/` exports → 727M
output rows (~21% duplicates removed). Produced by
`scripts/datasets/merge_partitions.py`. The overlap is expected: each `full/`
snapshot is a point-in-time recorder checkpoint published for reproducibility
and recovery, not an append-only delta.

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

## 15. Reproducibility

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

## 16. Limitations

OpenMarket has several limitations:

- WebSocket outages can create data gaps.
- Collector host clocks can drift.
- Top-of-book backtests may overestimate execution quality.
- Label definitions can leak information if settlement is mishandled.
- Simulated strategy outcomes may be sensitive to a small number of market regimes.
- Historical Polymarket liquidity may not match future liquidity.
- Live execution has additional latency, queue position, and partial-fill risks.
- BTC 15-minute markets are only one prediction-market domain; lower tick rates,
  wider books, ambiguous settlement, and missing external reference streams can
  break the assumptions used here.
- Step3 feature export writes 2,251 of 4,450 `market_meta` markets; skipped
  markets are mostly no Polymarket ticks (1,241), insufficient Binance trades
  (922), missing Binance partitions (21), no valid book snapshots (14), and one
  tied close.
- Full-archive `features/` Parquet is not published on Hugging Face; researchers
  should use `export_step3_from_parquet` on `unified/` instead.

## 17. Empirical Characterization

Regenerate stats: `paper/scripts/paper/analyze_unified.py` →
`paper/assets/stats/characterization.tex`.

**Scale (unified split, v0.4.3-unified):**

| Table | Rows |
|---|---:|
| `polymarket_ticks_ms` | 605,608,370 |
| `binance_trades` | 62,258,815 |
| `binance_ticks_ms` | 55,792,056 |
| `lag_pairs_ms` | 2,936,031 |
| `market_meta` | 4,450 |
| derived candle tables | 498,525 |
| **Total (11 tables)** | **727,098,247** |

On-disk size: 8.7 GiB. Collection span: 109 days (202 snapshots).
Counts and part numbers are tied to the frozen `v0.4.3-unified` manifest after
recovery and final deduplication; earlier draft tables generated from
pre-recovery subsets are not directly comparable.

**Lead–lag (`lag_pairs_ms`):** median 16 ms; 5th/95th percentiles -185 / +315 ms.

These statistics describe archival corpus content, not trading profitability.
Microstructure findings and forecast benchmarks are in Section 18.

## 18. Microstructure Findings

Regenerate: `paper/scripts/paper/analyze_research.py` →
`paper/assets/stats/research_findings.json` and figures under
`paper/assets/figures/`.

**Lead–lag vs. disagreement.** Median lead–lag is stable (16–19 ms) across
`|price_delta_bps|` quintiles—a null result. Daily pairing activity and median
lead–lag are weakly correlated; lead–lag magnitude does not predict contemporaneous
`|price_delta_bps|` (Pearson r ≈ 0 on 500k pairs).

**Spread stylized facts.** Top-of-book spreads concentrate at one tick wide
(median ≈ 0.01; 95th ≈ 0.02). Tight spreads imply mid-price backtests can
overstate executable edge.

**Forecast benchmarks (357,390 step3 rows):**

| Model | AUC | Brier |
|---|---:|---:|
| Naive `market_mid_prior_up` | 0.840 | 0.163 |
| Logistic + Platt (`v0.2.1`) | 0.841 | 0.163 |
| `drift_prob_up` only | 0.773 | 0.218 |
| `imbalance_60s` (sigmoid) | 0.586 | 0.246 |

ΔAUC ≈ 0.0014 vs. naive mid (bootstrap 95% CI [0.0013, 0.0015], p < 0.001):
statistically detectable, economically tiny. Brier rises from ~0.162 at one-tick
spreads to ~0.185 when spread ≥ 0.015; high-vol terciles also calibrate better
(0.160 vs. 0.164 low-vol).

## 19. Discussion

**What this enables.** Dubach [2026] establishes Polymarket microstructure stylized
facts; Saguillo et al. [2025] quantify arbitrage gaps. Neither provides a
reproducible Binance–Polymarket timeline with published pairing metadata and
walk-forward calibration baselines.

**Venue positioning.** Best read as a data-and-methods release with empirical
baselines—appropriate for arXiv, ML-for-finance workshops, or data-descriptor
journals.

**Domain scope.** BTC 15m markets are liquid and volatile but niche. Election,
macro, or long-dated markets can have lower tick rates, wider/intermittent
books, discrete news shocks, ambiguous settlement mechanics, and no single
external reference stream like BTC/USDT; those differences require
re-collection, re-synchronization, and re-calibration.

**Operational context.** Polygon settlement latency, oracle definition risk, and
regulatory uncertainty affect economic interpretation. Collector-host clock drift
and WebSocket gaps are documented; top-of-book backtests are not executable PnL
without explicit queue and fee models.

**Ethics / availability.** Public market data only (no user identities). Apache
2.0 code; cite dataset version and tag `v0.5.0` (see `CITATION.md`).

**Funding / competing interests.** Independent open-source release; no external
funding or competing interests are declared.

## 20. Conclusion

We presented OpenMarket, an open-source pipeline and public archive for
high-frequency Polymarket BTC 15-minute markets synchronized with Binance
BTC/USDT. The release includes 727.1M deduplicated events, 2.94M lead–lag pairs,
reproducible Rust exporters and trainers, and Hugging Face artifacts
(`v0.4.3-unified`, `v0.2.1/` model).

Initial analyses show tight Polymarket spreads, heavy-tailed lead–lag with a
compact median, and forecast benchmarks where multivariate logistic models offer
only modest gains over naive mid priors. We invite researchers to use the corpus
for microstructure and forecasting studies and cite the dataset version used in
each experiment.

## 21. Archive Closeout and Research Extensions

OpenMarket is no longer an active data-collection project. Archival closeout
completed on 2026-07-01:

- all 202 CDN manifest snapshots published in `full/` (`202 clean`, `0 partial`)
- five formerly-partial snapshots recovered via `sqlite3 .recover` and re-exported
- `unified/` rebuilt (`v0.4.3-unified`, 727M rows)
- `v0.2.1/` binary-outcome model published on Hugging Face Models
- unified backfill synced (`v0.4.3-unified`); source tag `v0.5.0` on private GitHub

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

## 22. Open Source Release

The public release includes:

- GitHub repository: `github.com/gregyoung14/openmarket` (private during pre-launch)
- Hugging Face dataset: `gregyoung14/openmarket-btc-polymarket`
- Hugging Face models: `gregyoung14/openmarket-models` (`v0.2.1/` walk-forward
  logistic on unified step3; `v0.2/`, `v0.1/` historical)
- mdBook documentation
- Rust API documentation
- Docker reproducibility
- benchmark tables
- Apache License 2.0 (code) and documented HF dataset license
- Jupyter quickstart: `notebooks/quickstart.ipynb`
- contribution guide
- GitHub issues labeled by contribution area (assets, exchanges, models)

## 23. References

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

## Appendix B. Workspace Crate Index

See `paper/sections/appendix-architecture.tex` for the full Rust workspace crate
list including `step3-parquet-export` and `binary-outcome-trainer`.

## Appendix C. Figure Specifications

Figures are generated by `paper/scripts/compile.sh` into `paper/assets/figures/`.

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
