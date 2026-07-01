# OpenMarket v0.5.0 — complete archive publication

v0.5.0 is the final archival release. It completes publication of the fixed
202-snapshot CDN inventory and rebuilds the recommended `unified/` split from
the full archive.

## What changed since v0.4.0

- **Dataset `full/`**: expanded from 10 to **202** published snapshots
  (3,258 parquet files, ~598M rows before dedupe).
- **Dataset `unified/`**: rebuilt as **`v0.4-unified`** (later refreshed to
  **`v0.4.2-unified`** after all partial recoveries) from the complete
  `full/` tree — **586,158,580 rows**, **467 parquet files**, **7.5 GB** on
  disk. Removed **12,118,682 duplicates** (~2.0 %) across 598,277,262 input
  rows.
- **Queue metadata**: `docs/release/full-snapshot-publish-status.json` reconciled
  (`202 published-clean`, `0 published-partial`, `0 corrupt` after sqlite3
  recovery of all five formerly-partial snapshots).
- **Docs**: README, dataset cards, paper, and `PROJECT-STATUS.md` updated to
  describe archival shutdown with complete coverage.

## Per-table coverage (unified/) — truth from parquet

| table | rows | parts |
|---|---:|---:|
| `binance_trades` | 61,825,064 | 54 |
| `binance_ticks_ms` | 47,639,344 | 47 |
| `polymarket_ticks_ms` | 473,361,665 | 46 |
| `lag_pairs_ms` | 2,931,191 | 7 |
| `binance_candles_1s` | 303,173 | 39 |
| `binance_candles_5s` | 82,050 | 56 |
| `binance_candles_1m` | 8,195 | 56 |
| `binance_candles_5m` | 2,155 | 56 |
| `binance_candles_15m` | 929 | 56 |
| `binance_candles_1h` | 364 | 49 |
| `market_meta` | 4,450 | 1 |
| `crossover_alerts` | 0 | 0 |

## Archive publication summary

| Metric | Value |
|---|---:|
| CDN manifest snapshots | 202 |
| `full/` export reports | 202 |
| `published-clean` queue | 202 |
| `published-partial` queue | 0 |
| Clean batches completed | 01–20 |

## Release artifacts

```text
Source tag:        v0.5.0
Source repo:       github.com/gregyoung14/openmarket
Dataset:           huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset versions:  v0.2-full (202 snapshots), v0.4-unified (deduped), v0.4-features
Models:            huggingface.co/gregyoung14/openmarket-models
Model version:     v0.1
Paper:             paper/paper.md
```

## What this release does not claim

- No ongoing data collection or model maintenance
- No claim of deployable production trading alpha
- All five formerly-partial snapshots (`03-22`, `03-29`, `04-10`, `04-21`,
  `05-14_003913`) were recovered via `sqlite3 .recover` and reclassified to
  `published-clean` post-release; queue now has zero partials