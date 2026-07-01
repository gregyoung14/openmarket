# OpenMarket

[![CI](https://github.com/gregyoung14/openmarket/actions/workflows/ci.yml/badge.svg)](https://github.com/gregyoung14/openmarket/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/gregyoung14/openmarket)](https://github.com/gregyoung14/openmarket/releases)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Dataset](https://img.shields.io/badge/HF-dataset-yellow.svg)](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)
[![Models](https://img.shields.io/badge/HF-models-yellow.svg)](https://huggingface.co/gregyoung14/openmarket-models)

OpenMarket is an open-source Rust research platform for collecting,
synchronizing, and backtesting high-frequency Polymarket prediction-market data
against Binance BTC market data.

The goal is reproducible prediction-market research, not a black-box trading
bot. The repository contains code, schemas, documentation, examples, and release
scripts. Large datasets and pretrained models are released separately through
Hugging Face and GitHub Releases.

## What This Project Provides

- Rust WebSocket collectors for Binance BTC/USDT trades and Polymarket CLOB
  order book events
- A market data recorder that stores millisecond-resolution ticks, market
  metadata, and Binance/Polymarket lead-lag pairs
- A reproducible backtesting engine for BTC 15-minute binary markets
- Strategy research modules covering drift, order-flow imbalance, scoreboard,
  whipsaw, volume gates, calibration, and Brier-score monitoring
- Legacy Python/ML research archive for XGBoost, LightGBM, SHAP, and stacked
  classifiers
- Dataset download scripts, schemas, and a Hugging Face release plan
- A systems-paper draft describing architecture, synchronization, data quality,
  feature engineering, reproducibility, and limitations

## Repository Layout

```text
openmarket/
├── crates/
│   ├── common/                # Shared Rust constants and types
│   ├── exchange-binance/      # Binance BTC WebSocket collector
│   ├── exchange-polymarket/   # Polymarket CLOB WebSocket collector
│   ├── recorder/              # Multi-market recorder and lag-pair exporter
│   ├── signal-engine/         # Real-time signal service
│   ├── execution-engine/      # Optional live/paper execution service
│   ├── paper-executor/        # Paper trading executor
│   ├── backtester/            # Reproducible historical backtester
│   ├── data-prep/             # Data conversion and preparation utilities
│   └── dataset-downloader/    # Snapshot downloader utilities
├── datasets/                  # Dataset cards, schemas, download scripts
├── docs/                      # Architecture, data, ML, release docs
├── examples/                  # Minimal reproducible examples
├── configs/                   # Safe example configs
├── docker/                    # Reproducible local runtime
├── benchmarks/                # Benchmark plans and harnesses
├── research/                  # Strategy evolution and legacy ML archive
├── paper/                     # Systems-paper draft
└── scripts/                   # Repo automation and release scripts
```

## Architecture

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
          ML / Signal
              |
              v
          Backtester
              |
              v
          Evaluation
```

## Quick Start

Clone and build the workspace:

```bash
git clone https://github.com/gregyoung14/openmarket.git
cd openmarket
cargo check --workspace
```

Load the published HF sample split (~204 KB, no large download):

```bash
python3 -m venv .venv && .venv/bin/pip install -r scripts/datasets/requirements.txt -r scripts/hf/requirements.txt
.venv/bin/python scripts/hf/validate_sample_split.py      # round-trip the sample
.venv/bin/python scripts/hf/benchmark_baseline.py         # record metrics
.venv/bin/pip install jupyter pandas matplotlib
.venv/bin/jupyter nbconvert --to notebook --execute notebooks/quickstart.ipynb
```

Run a backtest:

```bash
cargo run -p v15_brier_calibration --release -- --db-path data/openmarket.db
```

Run the local service stack (collector + recorder + dashboards):

```bash
cp configs/openmarket.example.env .env
docker compose -f docker/docker-compose.yml up
```

Legacy snapshot download (Bunny CDN; not the recommended public path):

```bash
python3 datasets/download.py --snapshot <snapshot-or-url> --out data/openmarket.db
```

## Datasets

Live on Hugging Face:

- Sample split (v0.1-sample): [gregyoung14/openmarket-btc-polymarket](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)
  — 12 tables, 9,352 rows, ~204 KB parquet, ~370 KB on disk
- Full split (planned v0.2-full): the 5 largest snapshots, ~45 GB compressed

Dataset partitions inside the HF repo:

```text
sample/
  binance_trades/date=YYYY-MM-DD/*.parquet
  binance_ticks_ms/date=YYYY-MM-DD/*.parquet
  polymarket_ticks_ms/date=YYYY-MM-DD/*.parquet
  lag_pairs_ms/date=YYYY-MM-DD/*.parquet
  binance_candles_{1s,5s,1m,5m,15m,1h}/date=YYYY-MM-DD/*.parquet
  market_meta/
  crossover_alerts/
metadata/
  snapshot_manifest.json
  snapshot_manifest.tsv
  per-snapshot export reports
README.md
```

See [datasets/README.md](datasets/README.md) and
[docs/data/dataset-release.md](docs/data/dataset-release.md).

## Models

Pretrained model artifacts are not committed to Git. They will live in:

- [gregyoung14/openmarket-models](https://huggingface.co/gregyoung14/openmarket-models)
  — currently scaffolded with `.gitattributes` + `README.md`; first v0.1.0 model
  is **deferred** to a future release.

The repository keeps model metadata, feature schemas, training code, and release
manifests.

## Benchmarks

Planned benchmark categories:

- WebSocket message throughput
- Tick normalization latency
- Lead-lag pairing throughput
- Feature generation speed
- Backtest wall-clock time
- Inference latency
- Memory and CPU usage

See [benchmarks/README.md](benchmarks/README.md).

## Documentation

- [Architecture](docs/architecture/overview.md)
- [Synchronization](docs/data/synchronization.md)
- [Dataset release plan](docs/data/dataset-release.md)
- [ML pipeline](docs/ml/pipeline.md)
- [Reproducibility](docs/reproducibility.md)
- [Release process](docs/release/releases.md)
- [Systems paper](paper/paper.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).

This license permits commercial use, modification, and redistribution while
preserving attribution and patent protections.
