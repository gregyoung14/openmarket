---
configs:
  - config_name: default
    data_files:
      - split: sample
        path: sample/*.parquet
dataset_info:
  description: |
    High-frequency research dataset pairing Binance BTC/USDT market data with
    Polymarket BTC binary-market order book events. The published sample split
    covers a single SQLite snapshot from the operator archive and is intended
    for tests, CI, and quickstarts. The full multi-gigabyte archive is released
    incrementally through additional splits.
  features:
    - name: binance_trades
      description: Binance BTC/USDT spot trades streamed from `wss://stream.binance.com:9443/ws/btcusdt@trade`
      columns:
        - {name: trade_id, dtype: int64}
        - {name: trade_time, dtype: int64, description: 'exchange trade timestamp (ms since epoch)'}
        - {name: price, dtype: float64}
        - {name: quantity, dtype: float64}
        - {name: quote_volume, dtype: float64}
        - {name: is_buyer_maker, dtype: bool}
        - {name: received_at, dtype: int64, description: 'collector ingest timestamp (ms since epoch)'}
        - {name: date, dtype: string, description: 'UTC date partition derived from trade_time'}
    - name: binance_ticks_ms
      description: Binance top-of-book millisecond snapshots from the order-book stream
      columns:
        - {name: id, dtype: int64}
        - {name: source_ts_ms, dtype: int64}
        - {name: ingest_ts_ms, dtype: int64}
        - {name: market_slug, dtype: string}
        - {name: price, dtype: float64}
        - {name: best_bid, dtype: float64}
        - {name: best_ask, dtype: float64}
        - {name: date, dtype: string}
    - name: polymarket_ticks_ms
      description: Polymarket CLOB top-of-book millisecond snapshots for the corresponding BTC binary market
      columns:
        - {name: id, dtype: int64}
        - {name: source_ts_ms, dtype: int64}
        - {name: ingest_ts_ms, dtype: int64}
        - {name: market_slug, dtype: string}
        - {name: asset_id, dtype: string}
        - {name: side_label, dtype: string, description: '"up" or "down"' }
        - {name: event_type, dtype: string}
        - {name: price, dtype: float64}
        - {name: best_bid, dtype: float64}
        - {name: best_ask, dtype: float64}
        - {name: size, dtype: float64}
        - {name: paired, dtype: bool}
        - {name: date, dtype: string}
    - name: lag_pairs_ms
      description: Paired Binance/Polymarket events with computed lead/lag in milliseconds
      columns:
        - {name: id, dtype: int64}
        - {name: paired_at_ms, dtype: int64}
        - {name: market_slug, dtype: string}
        - {name: side_label, dtype: string}
        - {name: binance_tick_id, dtype: int64}
        - {name: polymarket_tick_id, dtype: int64}
        - {name: binance_source_ts_ms, dtype: int64}
        - {name: polymarket_source_ts_ms, dtype: int64}
        - {name: lead_lag_ms, dtype: int64, description: 'polymarket_source_ts_ms - binance_source_ts_ms'}
        - {name: binance_price, dtype: float64}
        - {name: polymarket_bid, dtype: float64}
        - {name: price_delta_bps, dtype: float64}
        - {name: quality_flag, dtype: int64}
        - {name: date, dtype: string}
    - name: binance_candles_1s
      description: Binance 1-second aggregated candles for BTC/USDT
      columns:
        - {name: candle_start, dtype: int64}
        - {name: open, dtype: float64}
        - {name: high, dtype: float64}
        - {name: low, dtype: float64}
        - {name: close, dtype: float64}
        - {name: volume, dtype: float64}
        - {name: quote_volume, dtype: float64}
        - {name: trades, dtype: int64}
        - {name: date, dtype: string}
    - name: binance_candles_5s
      description: Binance 5-second aggregated candles
      columns: &candle_cols
        - {name: candle_start, dtype: int64}
        - {name: open, dtype: float64}
        - {name: high, dtype: float64}
        - {name: low, dtype: float64}
        - {name: close, dtype: float64}
        - {name: volume, dtype: float64}
        - {name: quote_volume, dtype: float64}
        - {name: trades, dtype: int64}
        - {name: date, dtype: string}
    - name: binance_candles_1m
      description: Binance 1-minute aggregated candles
      columns: *candle_cols
    - name: binance_candles_5m
      description: Binance 5-minute aggregated candles
      columns: *candle_cols
    - name: binance_candles_15m
      description: Binance 15-minute aggregated candles
      columns: *candle_cols
    - name: binance_candles_1h
      description: Binance 1-hour aggregated candles
      columns: *candle_cols
    - name: market_meta
      description: Polymarket market metadata for the BTC binary markets referenced in this snapshot
      columns:
        - {name: slug, dtype: string}
        - {name: condition_id, dtype: string}
        - {name: question, dtype: string}
        - {name: end_date_iso, dtype: string}
        - {name: up_token_id, dtype: string}
        - {name: down_token_id, dtype: string}
        - {name: resolved_outcome, dtype: string}
        - {name: closed, dtype: bool}
        - {name: date, dtype: string, description: 'always "unpartitioned"'}
    - name: crossover_alerts
      description: Reserved table; always empty in this snapshot.
      columns: []
  splits:
    - name: sample
      num_examples: 9352
      num_bytes: 204401
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
  - 1K<n<10K
---

# OpenMarket BTC Polymarket

OpenMarket BTC Polymarket is a high-frequency research dataset pairing Binance
BTC/USDT market data with Polymarket BTC binary-market order book events.

The dataset is released to support reproducible prediction-market research,
feature engineering, market microstructure analysis, and backtesting.

## Repository Layout

```text
sample/
  binance_trades.parquet
  binance_ticks_ms.parquet
  polymarket_ticks_ms.parquet
  lag_pairs_ms.parquet
  binance_candles_1s.parquet
  binance_candles_5s.parquet
  binance_candles_1m.parquet
  binance_candles_5m.parquet
  binance_candles_15m.parquet
  binance_candles_1h.parquet
  market_meta.parquet
  crossover_alerts.parquet      # reserved; empty in this snapshot
metadata/
  snapshot_manifest.json        # full archive inventory (URLs redacted)
  snapshot_manifest.tsv         # same, TSV
  <snapshot>.export_report.json # per-snapshot export integrity report
README.md
```

The flat layout (one parquet file per table under `sample/`) is what the
Hugging Face Data Studio viewer auto-converts. Each parquet file includes a
`date` column (UTC, `YYYY-MM-DD`) so downstream code can filter by day
without relying on Hive-style partition segments.

## Current Sample

The first sample export uses:

```text
polymarket_btc_data_2026-05-14_145928.db.gz
```

SQLite integrity check: `ok`.

Rows exported per table:

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

## Full Snapshot Inventory

The operator archive currently contains 202 SQLite snapshot files from
`2026-03-14T19:32:15Z` through `2026-07-01T02:56:54Z`, totaling
46,205,325,113 compressed bytes across 5 large snapshots (≥1 GB each) and
192 smaller post-prune residue snapshots. The full inventory is published
in `metadata/snapshot_manifest.{json,tsv}` with the operator's storage
hostname redacted to `cdn.example.com`.

The full public Parquet release will populate the `full/` split
incrementally via `scripts/hf/release_split.py` once per-snapshot export
quality is validated.

## Timestamp Semantics

- `source_ts_ms`: timestamp from the exchange or source event (ms since epoch, UTC)
- `ingest_ts_ms`: timestamp observed by the collector (ms since epoch, UTC)
- `lead_lag_ms = polymarket_source_ts_ms - binance_source_ts_ms`

Positive `lead_lag_ms` values indicate that the Polymarket event timestamp
follows the Binance event timestamp.

## Limitations

- WebSocket reconnects can create gaps.
- Collector host clocks can drift.
- Raw JSON columns are excluded from the default Parquet export.
- Top-of-book backtests may overstate executable fill quality.
- The initial sample is intentionally small and is not representative of
  full historical coverage.
- This snapshot is a point-in-time export; re-running the export pipeline on
  a later snapshot may produce slightly different row counts if the
  underlying recorder continued collecting between exports.

## Release artifacts

```text
Source repo:     github.com/gregyoung14/openmarket
Dataset:         huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version: v0.1-sample
Models:          huggingface.co/gregyoung14/openmarket-models
Model version:   deferred; model-card scaffold uploaded
```

## Citation

If you use OpenMarket, cite the GitHub repository and the dataset version
used in your experiment.