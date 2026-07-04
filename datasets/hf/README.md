---
license: apache-2.0
task_categories:
  - tabular-classification
  - time-series-forecasting
tags:
  - prediction-markets
  - polymarket
  - binance
  - bitcoin
  - market-microstructure
  - high-frequency-data
pretty_name: OpenMarket BTC Polymarket
size_categories:
  - 100M<n<1B
---

# OpenMarket BTC Polymarket

OpenMarket BTC Polymarket is a high-frequency research dataset pairing
Binance BTC/USDT market data with Polymarket BTC binary-market order book
events.

The dataset is released to support reproducible prediction-market
research, feature engineering, market microstructure analysis, and
backtesting.

## How to Cite

```bibtex
@misc{openmarket_dataset_2026,
  title        = {{OpenMarket BTC Polymarket}: Synchronized High-Frequency Corpus},
  author       = {Young, Gregory},
  year         = {2026},
  howpublished = {\url{https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket}},
  note         = {Version v0.4.3-unified; source tag v0.5.2}
}
```

Full entries: [CITATION.md](https://github.com/gregyoung14/openmarket/blob/main/CITATION.md).

## Repository Layout

```text
binance_trades.parquet         # v0.1-sample (flat at repo root for Data Studio)
binance_ticks_ms.parquet
polymarket_ticks_ms.parquet
lag_pairs_ms.parquet
binance_candles_{1s,5s,1m,5m,15m,1h}.parquet
market_meta.parquet
crossover_alerts.parquet
full/                          # complete 202-snapshot archive (v0.2-full)
  <table>/date=YYYY-MM-DD/*.parquet
  metadata/<snapshot>.export_report.json
unified/                       # deduped research timeline (v0.4.3-unified)
  <table>/date=YYYY-MM-DD/*.parquet
  metadata/merge_quality_report.json
features/                      # optional ML exports (v0.4-features; sample demo on HF)
  step2_100ms/
  step2_1s/
  step3_binary_calibration/
metadata/
  snapshot_manifest.json       # full archive inventory (CDN URLs redacted)
  snapshot_manifest.tsv
README.md
```

Each split uses Hive-style `date=YYYY-MM-DD` partitions under table
directories. Parquet files also include a `date` column (UTC,
`YYYY-MM-DD`) for downstream filtering.

OpenMarket is in archival shutdown. No new snapshots will be collected;
this repository is a fixed public research record frozen at source tag
`v0.5.2`.

## Published Splits

The operator archive contains 202 SQLite snapshot files from
`2026-03-14T19:32:15Z` through `2026-07-01T02:56:54Z`. The inventory
is in `metadata/snapshot_manifest.{json,tsv}` (CDN hostname redacted to
`cdn.example.com`).

| Split | Version | Description |
|---|---|---|
| `unified/` | v0.4.3-unified | **Recommended.** Deduped timeline (~727M rows, 504 parquet files) |
| `full/` | v0.2-full | Complete 202-snapshot per-export archive (3,312 parquet files) |
| (repo root) | v0.1-sample | Tiny demo split for CI and quickstarts (12 flat parquet, 9,352 rows) |
| `features/` | v0.4-features | **Optional.** Step2/step3 demo (one snapshot on HF); full features reproducible from `unified/` |

### `unified/` backfill (`v0.4.3-unified`)

Adds five date partitions missing from `v0.4.2-unified`, sourced from staging
SQLite via `unified-backfill sqlite-fill`:

- `binance_trades/date=2026-03-23` (~191k rows)
- `binance_trades/date=2026-05-15`
- `binance_ticks_ms/date=2026-05-15`
- `polymarket_ticks_ms/date=2026-05-15`
- `lag_pairs_ms/date=2026-05-15`

Provenance: `unified/metadata/sqlite_fill.json`. April 22–May 12 remains a
collection gap with no recoverable archive data.

### Features split (`v0.4-features`) — optional

A full-archive `features/` upload is **not required** for the public research
record. The recommended path is to export step2/step3 features from `unified/`
Parquet using the published Rust and Python tooling (same pipeline that produced
the `v0.2.1/` model). Hugging Face currently hosts a **one-snapshot demo** (2
parquet files) for schema reference.

Reproduce from `unified/`:

```bash
cargo build -p step3-parquet-export -p binary-outcome-trainer --release
./target/release/export_step3_from_parquet \
  --parquet-root data/hf_release/unified_parquet \
  --out-dir data/hf_release/features_exports
```

Per-snapshot step2/step3 via SQLite (legacy path):

```bash
.venv/bin/python scripts/datasets/export_ml_features.py \
  --snapshot polymarket_btc_data_2026-03-14_193215.db.gz
```

See `scripts/ml/README.md` in the source repo.

Load the unified split:

```python
from huggingface_hub import snapshot_download
root = snapshot_download(
    "gregyoung14/openmarket-btc-polymarket",
    repo_type="dataset",
    allow_patterns=["unified/**", "metadata/**", "README.md"],
)
```

### Sample split (`v0.1-sample`)

Published as **12 flat `*.parquet` files at the repository root** (not under
`sample/`) so the Hugging Face Data Studio viewer can index them without
traversing a split subdirectory. Download with `allow_patterns=["*.parquet",
"metadata/**"]` or use `datasets/download.py --split sample`.

Source snapshot: `polymarket_btc_data_2026-05-14_145928.db.gz` (SQLite
integrity check: `ok`).

| Table | Rows |
|---|---:|
| `binance_trades` | 4,249 |
| `binance_ticks_ms` | 1,044 |
| `polymarket_ticks_ms` | 2,000 |
| `lag_pairs_ms` | 1,769 |
| `binance_candles_1s` | 9 |
| `binance_candles_5s` | 1 |
| `binance_candles_1m` | 104 |
| `binance_candles_5m` | 23 |
| `binance_candles_15m` | 5 |
| `binance_candles_1h` | 2 |
| `market_meta` | 146 |
| `crossover_alerts` | 0 |
| **Total** | **9,352** |

## Schema

Each parquet file is a flat table; column dtypes are inferred from the
file and surfaced by `huggingface_hub.list_repo_files` /
`pyarrow.parquet.read_schema`. A reference schema follows:

### `binance_trades`

| column | dtype | description |
|---|---|---|
| `trade_id` | int64 | Binance trade ID |
| `trade_time` | int64 | exchange trade timestamp (ms since epoch, UTC) |
| `price` | float64 | trade price (USDT per BTC) |
| `quantity` | float64 | trade quantity (BTC) |
| `quote_volume` | float64 | trade quantity * price (USDT) |
| `is_buyer_maker` | bool | true if buyer was the maker side |
| `received_at` | int64 | collector ingest timestamp (ms since epoch, UTC) |
| `date` | string | UTC date partition derived from `trade_time` |

### `binance_ticks_ms`

| column | dtype | description |
|---|---|---|
| `id` | int64 | synthetic ID |
| `source_ts_ms` | int64 | source event timestamp (ms since epoch, UTC) |
| `ingest_ts_ms` | int64 | collector ingest timestamp (ms since epoch, UTC) |
| `market_slug` | string | related Polymarket market slug |
| `price` | float64 | Binance mid price |
| `best_bid` | float64 | Binance top bid |
| `best_ask` | float64 | Binance top ask |
| `date` | string | UTC date partition derived from `source_ts_ms` |

### `polymarket_ticks_ms`

| column | dtype | description |
|---|---|---|
| `id` | int64 | synthetic ID |
| `source_ts_ms` | int64 | source event timestamp (ms since epoch, UTC) |
| `ingest_ts_ms` | int64 | collector ingest timestamp (ms since epoch, UTC) |
| `market_slug` | string | Polymarket market slug |
| `asset_id` | string | conditional token ID |
| `side_label` | string | `"up"` or `"down"` |
| `event_type` | string | Polymarket event type |
| `price` | float64 | mid price |
| `best_bid` | float64 | top bid |
| `best_ask` | float64 | top ask |
| `size` | float64 | depth at top of book |
| `paired` | bool | true if this tick was paired with a Binance tick |
| `date` | string | UTC date partition derived from `source_ts_ms` |

### `lag_pairs_ms`

| column | dtype | description |
|---|---|---|
| `id` | int64 | synthetic ID |
| `paired_at_ms` | int64 | pairing timestamp (ms since epoch, UTC) |
| `market_slug` | string | Polymarket market slug |
| `side_label` | string | `"up"` or `"down"` |
| `binance_tick_id` | int64 | FK → `binance_ticks_ms.id` |
| `polymarket_tick_id` | int64 | FK → `polymarket_ticks_ms.id` |
| `binance_source_ts_ms` | int64 | paired Binance source timestamp |
| `polymarket_source_ts_ms` | int64 | paired Polymarket source timestamp |
| `lead_lag_ms` | int64 | `polymarket_source_ts_ms - binance_source_ts_ms` |
| `binance_price` | float64 | Binance mid at pairing time |
| `polymarket_bid` | float64 | Polymarket bid at pairing time |
| `price_delta_bps` | float64 | price differential in basis points |
| `quality_flag` | int64 | pairing quality flag (0 = good) |
| `date` | string | UTC date partition derived from `paired_at_ms` |

### `binance_candles_{1s,5s,1m,5m,15m,1h}`

| column | dtype | description |
|---|---|---|
| `candle_start` | int64 | candle start timestamp (ms since epoch, UTC) |
| `open` | float64 | open price |
| `high` | float64 | high price |
| `low` | float64 | low price |
| `close` | float64 | close price |
| `volume` | float64 | base asset volume (BTC) |
| `quote_volume` | float64 | quote asset volume (USDT) |
| `trades` | int64 | number of trades in the candle |
| `date` | string | UTC date partition derived from `candle_start` |

### `market_meta`

| column | dtype | description |
|---|---|---|
| `slug` | string | Polymarket market slug |
| `condition_id` | string | on-chain condition ID |
| `question` | string | market question |
| `end_date_iso` | string | market close time (ISO 8601) |
| `up_token_id` | string | conditional token ID for "up" outcome |
| `down_token_id` | string | conditional token ID for "down" outcome |
| `resolved_outcome` | string | winning outcome, if resolved |
| `closed` | bool | true if market is closed |
| `date` | string | always `"unpartitioned"` |

### `crossover_alerts`

Empty in this snapshot; reserved for the strategy alert table.

## Timestamp Semantics

- `source_ts_ms`: timestamp from the exchange or source event (ms since epoch, UTC)
- `ingest_ts_ms`: timestamp observed by the collector (ms since epoch, UTC)
- `lead_lag_ms = polymarket_source_ts_ms - binance_source_ts_ms`

Positive `lead_lag_ms` values indicate that the Polymarket event
timestamp follows the Binance event timestamp.

## Limitations

- WebSocket reconnects can create gaps.
- Collector host clocks can drift.
- Raw JSON columns are excluded from the default Parquet export.
- Top-of-book backtests may overstate executable fill quality.
- The initial sample is intentionally small and is not representative
  of full historical coverage.
- This snapshot is a point-in-time export; re-running the export
  pipeline on a later snapshot may produce slightly different row
  counts if the underlying recorder continued collecting between
  exports.

## Release artifacts

```text
Source tag:      v0.5.2
Source repo:     github.com/gregyoung14/openmarket
Dataset:         huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version: v0.4.3-unified
Models:          huggingface.co/gregyoung14/openmarket-models
Model version:   v0.2.1 (recommended; walk-forward logistic on unified step3)
                 v0.2 / v0.1 (historical)
```

## Citation

If you use OpenMarket, cite the GitHub repository and the dataset
version used in your experiment.
