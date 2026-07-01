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

OpenMarket is now in archival shutdown. No new live data will be collected. The
complete 202-snapshot CDN archive is published on Hugging Face; the project is
frozen as a public research record.

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

Download the unified research dataset (recommended):

```bash
.venv/bin/python datasets/download.py --split unified --out data/hf_cache
```

Legacy operator CDN snapshots (migration only):

```bash
python3 datasets/download.py --legacy-cdn sample --out data/openmarket.db
```

## Datasets

Live on Hugging Face ([gregyoung14/openmarket-btc-polymarket](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)):

| Split | Version | Use |
|---|---|---|
| `unified/` | v0.4-unified | **Recommended** — deduped timeline from complete archive |
| `features/` | v0.4-features | ML features — step2 (100ms/1s) + step3 binary calibration |
| `full/` | v0.2-full | Complete 202-snapshot per-export archive |
| `sample/` | v0.1-sample | CI, quickstarts — 12 tables, 9,352 rows, ~204 KB |

```text
unified/                     # deduped research timeline (v0.4+)
full/                        # per-snapshot exports, 202 snapshots (v0.2+)
sample/                      # tiny demo split (v0.1)
metadata/
  snapshot_manifest.json     # full archive inventory (CDN URLs redacted)
  merge_quality_report.json  # unified dedupe stats
  per-snapshot export reports
README.md
```

See [datasets/README.md](datasets/README.md) and
[docs/data/dataset-release.md](docs/data/dataset-release.md).

Archival status:

- 202 SQLite snapshots inventoried in the redacted manifest; all 202 published
  in `full/` (`197 clean`, `5 partial` table exports).
- `unified/` (`v0.4-unified`) deduped from the complete `full/` tree — 586M
  rows, 467 parquet files.
- See `docs/release/PROJECT-STATUS.md` for queue metadata and closeout notes.

## Models

Pretrained model artifacts are not committed to Git. They live in:

- [gregyoung14/openmarket-models](https://huggingface.co/gregyoung14/openmarket-models)
  — public `v0.1/` artifacts are available, including a calibrated
  binary-outcome scorer and metrics snapshots.

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
