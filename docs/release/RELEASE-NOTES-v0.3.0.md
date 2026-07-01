# OpenMarket v0.3.0 — unified deduped dataset on Hugging Face

v0.3.0 merges the 10 overlapping `full/` snapshot exports into a single
deduplicated `unified/` split on Hugging Face. This is the recommended
research timeline; `full/` remains available for per-snapshot provenance.

## What changed since v0.2.0

- **Dataset**: new `unified/` split — **446,897,798 rows**, **431 Parquet files**,
  **5.73 GB** on disk. Merged from 456,026,287 input rows across 10 `full/`
  snapshot exports; removed **9,128,489 duplicates** (~2.0 %).
- **Tools**: added `scripts/datasets/merge_partitions.py` (DuckDB per-date
  dedupe using keys from the release investigation doc).
- **Tools**: `release_split.py`, `upload_split.py`, and
  `aggregate_export_reports.py` now support `--split unified`.
- **Docs**: Hugging Face is the canonical public download path; Bunny CDN
  relegated to legacy operator backup.
- **Repo**: GitHub visibility flipped to public.

## Per-table coverage (unified/) — truth from parquet

| table | rows | parts |
|---|---:|---:|
| `binance_trades` | 55,599,238 | 46 |
| `binance_ticks_ms` | 41,475,453 | 37 |
| `polymarket_ticks_ms` | 346,555,258 | 37 |
| `lag_pairs_ms` | 2,906,608 | 7 |
| `binance_candles_1s` | 263,151 | 31 |
| `binance_candles_5s` | 82,048 | 56 |
| `binance_candles_1m` | 8,173 | 56 |
| `binance_candles_5m` | 2,152 | 56 |
| `binance_candles_15m` | 927 | 55 |
| `binance_candles_1h` | 364 | 49 |
| `market_meta` | 4,426 | 1 |
| `crossover_alerts` | 0 | 0 |

## Dedupe keys

| table | key |
|---|---|
| `binance_trades` | `trade_id` |
| `binance_ticks_ms` | `(source_ts_ms, trade_time_ms, price, volume)` |
| `polymarket_ticks_ms` | `(source_ts_ms, market_slug, asset_id, side_label, event_type, price, best_bid, best_ask, size)` |
| `lag_pairs_ms` | `(paired_at_ms, market_slug, side_label, binance_source_ts_ms, polymarket_source_ts_ms, polymarket_bid)` |
| `binance_candles_*` | `candle_start` |
| `market_meta` | `market_slug` (keep latest `last_seen_ms`) |

## Release artifacts

```text
Source tag:        v0.3.0
Source repo:       github.com/gregyoung14/openmarket
Dataset:           huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version:   v0.3-unified
Models:            huggingface.co/gregyoung14/openmarket-models
Model version:     v0.1
Paper:             paper/paper.md
```

## Archive-closeout follow-ups

- Publish the remaining snapshots from the fixed 202-snapshot CDN archive.
- Reconcile queue metadata so published coverage exactly matches HF state.
- Re-export corrupt partial snapshots if recoverable; otherwise classify them as
  permanently lost.
