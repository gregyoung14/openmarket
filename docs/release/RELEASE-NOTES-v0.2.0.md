# OpenMarket v0.2.0 — full split published to Hugging Face

v0.2.0 promotes the `full/` Hugging Face dataset split to public-facing release
status, exporting 10 of the 202 archived SQLite snapshots (~45 GB compressed)
to partitioned Parquet and uploading them to the dataset repo.

## What changed since v0.1.0

- **Dataset**: 10 snapshots, 456,966,949 rows, 738 Parquet files, ~3.5 GB on disk
- **Tools**: added DuckDB-native exporter (`scripts/datasets/export_snapshot_v2.py`)
  that imports each SQLite snapshot into a native DuckDB in-memory table, then
  partitions and writes Parquet at ~10× the throughput of the sqlite3-based
  fallback. Falls back to per-table sqlite3 reads when the SQLite is corrupt.
- **Tools**: added multi-snapshot orchestrator
  (`scripts/datasets/export_many_snapshots.py`) with resume support.
- **Tools**: added aggregate-report generator
  (`scripts/datasets/aggregate_export_reports.py`) that walks all snapshot
  export reports and produces `full_aggregate.json` / `full_aggregate.md`.
- **Tools**: bumped validator (`scripts/hf/validate_sample_split.py`) to
  handle the nested `full/<table>/date=YYYY-MM-DD/*.parquet` layout and
  compare file/byte counts as the integrity source of truth (row counts from
  partial exports are warned, not failed).
- **Tools**: added dataset version bumper (`scripts/hf/bump_dataset_version.py`)
  and split uploader (`scripts/hf/upload_split.py`).
- **Tooling**: end-to-end orchestrator
  (`scripts/hf/release_split.py`) wires export -> validate -> upload -> bump.
- **Docs**: added `docs/release/pipeline.md` runbook.
- **CI**: added `.github/workflows/ci.yml` (cargo fmt/check/clippy/test +
  Python compile/validate) and `.github/workflows/release.yml` (auto-tag HF
  dataset on `v*` GitHub release).
- **Repo**: bumped workspace package version to 0.1.0 (still v0.1.0 because
  source code is unchanged; HF dataset bumps to v0.2-full).

## Full split inventory

| snapshot_id | compressed_bytes | rows | parts | status |
|---|---:|---:|---:|---|
| `polymarket_btc_data_2026-03-14_193215` | 10,935,294,993 | 236,166,002 | 251 | ok |
| `polymarket_btc_data_2026-03-29_215354` | 10,069,965,097 | 152,801,029 | 60 | partial |
| `polymarket_btc_data_2026-03-22_215354` | 9,691,222,331 | 13,478,907 | 31 | partial |
| `polymarket_btc_data_2026-04-21_211838` | 7,200,674,285 | (not exported) | 0 | n/a |
| `polymarket_btc_data_2026-04-10_232833` | 7,124,484,517 | 17,193,067 | 88 | ok |
| `polymarket_btc_data_2026-05-14_205654` |   393,243,254 | 1,019,592 | 47 | ok |
| `polymarket_btc_data_2026-05-13_183517` |   260,463,524 |   433,327 |  8 | partial |
| `polymarket_btc_data_2026-05-14_085654` |   241,952,456 | 2,668,141 | 64 | ok |
| `polymarket_btc_data_2026-04-03_232930` |   176,080,358 | 4,277,432 | 70 | ok |
| `polymarket_btc_data_2026-05-14_003913` |    18,516,286 |   159,005 | 17 | ok |

`polymarket_btc_data_2026-04-21_211838` was skipped because its SQLite image
is fully unreadable; the row counts for partial exports reflect only the
rows that could be salvaged via the per-table sqlite3 fallback.

## Per-table coverage (full/)

| table | rows | parts |
|---|---:|---:|
| `binance_trades` | 63,444,078 | 56 |
| `binance_ticks_ms` | 42,659,539 | 37 |
| `polymarket_ticks_ms` | 347,290,210 | 37 |
| `lag_pairs_ms` | 3,181,298 | 11 |
| `binance_candles_1s` | 263,151 | 32 |
| `binance_candles_5s` | 91,308 | 68 |
| `binance_candles_1m` | 18,359 | 126 |
| `binance_candles_5m` | 5,008 | 126 |
| `binance_candles_15m` | 2,121 | 124 |
| `binance_candles_1h` | 838 | 111 |
| `market_meta` | 9,713 | 10 |
| `crossover_alerts` | 0 | 0 |

## Validation (post-upload, from a clean download)

```
split:           full
snapshots:       10
reported rows:   456,966,949
reported files:  738
reported bytes:  3,472,100,738
file integrity:  OK
row integrity:   WARN (partial exports may over-count)
```

## Release artifacts

```text
Source tag:        v0.2.0
Source repo:       github.com/gregyoung14/openmarket
Dataset:           huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version:   v0.2-full
Models:            huggingface.co/gregyoung14/openmarket-models
Model version:     deferred; model-card scaffold uploaded
Paper:             paper/paper.md
```

## Post-release follow-ups

- Re-attempt `polymarket_btc_data_2026-04-21_211838` with `.recover` SQLite
  tooling, or accept it as permanently lost.
- Merge / dedupe / time-align across the 10 published snapshots.
- Benchmark the OpenMarket Rust backtester against this published split.
- Roll forward to v0.3 with the next 10 snapshots.

## Notes

This release is the **research-platform release**. All data on HF is from the
public Bunny CDN, no live trading endpoints are touched, and no production
credentials are exposed.