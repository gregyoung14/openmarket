---
dataset_info:
  features:
    - name: source_ts_ms
      dtype: int64
    - name: ingest_ts_ms
      dtype: int64
  splits:
    - name: sample
      num_examples: 9352
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
---

# OpenMarket BTC Polymarket

OpenMarket BTC Polymarket is a high-frequency research dataset pairing Binance
BTC/USDT market data with Polymarket BTC binary-market order book events.

The dataset is released to support reproducible prediction-market research,
feature engineering, market microstructure analysis, and backtesting.

## Repository Layout

```text
sample/
  binance_trades/date=YYYY-MM-DD/*.parquet
  binance_ticks_ms/date=YYYY-MM-DD/*.parquet
  polymarket_ticks_ms/date=YYYY-MM-DD/*.parquet
  lag_pairs_ms/date=YYYY-MM-DD/*.parquet
  binance_candles_*/date=YYYY-MM-DD/*.parquet
  market_meta/date=unpartitioned/*.parquet
  metadata/*.json
full/
  ...
metadata/
  snapshot_manifest.json
  snapshot_manifest.tsv
```

## Current Sample

The first sample export uses:

```text
polymarket_btc_data_2026-05-14_145928.db.gz
```

SQLite integrity check: `ok`.

Rows exported:

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

## Full Snapshot Inventory

The Bunny archive currently contains 202 SQLite snapshot files from
`2026-03-14T19:32:15Z` through `2026-07-01T02:56:54Z`, totaling
46,205,325,113 compressed bytes.

The full public Parquet release should prioritize the five large snapshots,
which contain approximately 45.022 GB compressed source data, then validate the
small post-prune snapshots separately.

## Timestamp Semantics

- `source_ts_ms`: timestamp from the exchange or source event
- `ingest_ts_ms`: timestamp observed by the collector
- `lead_lag_ms = polymarket_source_ts_ms - binance_source_ts_ms`

Positive lead-lag values indicate that the Polymarket event timestamp follows
the Binance event timestamp.

## Limitations

- WebSocket reconnects can create gaps.
- Collector host clocks can drift.
- Raw JSON columns are excluded from the default Parquet export.
- Top-of-book backtests may overstate executable fill quality.
- The initial sample is intentionally small and is not representative of full
  historical coverage.

## Release artifacts

```text
Source repo:     github.com/gregyoung14/openmarket
Dataset:         huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version: v0.1-sample
Models:          huggingface.co/gregyoung14/openmarket-models
Model version:   deferred; model-card scaffold uploaded
```

## Citation

If you use OpenMarket, cite the GitHub repository and the dataset version used
in your experiment.
