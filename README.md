# OpenMarket

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

Download a dataset snapshot:

```bash
python3 datasets/download.py --snapshot sample --out data/openmarket.db
```

Run a backtest:

```bash
cargo run -p v15_brier_calibration --release -- \
  --db-path data/openmarket.db
```

Run the local service stack:

```bash
cp configs/openmarket.example.env .env
docker compose -f docker/docker-compose.yml up
```

## Datasets

The Git repository intentionally does not contain large market data.

Planned public dataset:

```text
huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
```

Dataset partitions:

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

See [datasets/README.md](datasets/README.md) and
[docs/data/dataset-release.md](docs/data/dataset-release.md).

## Models

Pretrained model artifacts are not committed to Git. They belong in:

```text
huggingface.co/gregyoung14/openmarket-models
```

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
