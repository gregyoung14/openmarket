# OpenMarket v0.2.0 — full split published to Hugging Face

v0.2.0 promotes the `full/` Hugging Face dataset split to public-facing release
status, exporting 10 of the 202 archived SQLite snapshots (~45 GB compressed)
to partitioned Parquet and uploading them to the dataset repo.

## What changed since v0.1.0

- **Dataset**: 10 snapshots, **456,026,287 rows (from parquet), 3,472,100,738 bytes (3.23 GiB)**, 738 Parquet files on disk.
- **Tools**: added DuckDB-native exporter (`scripts/datasets/export_snapshot_v2.py`)
  that imports each SQLite snapshot into a native DuckDB in-memory table, then
  partitions and writes Parquet at ~10× the throughput of the sqlite3-based
  fallback. Falls back to per-table sqlite3 reads when the SQLite is corrupt.
- **Tools**: added multi-snapshot orchestrator
  (`scripts/datasets/export_many_snapshots.py`) with resume support.
- **Tools**: added aggregate-report generator
  (`scripts/datasets/aggregate_export_reports.py`) that walks all snapshot
  export reports and produces `full_aggregate.json` / `full_aggregate.md`.
  v0.2.0 update: aggregate now scans parquet files for ground-truth row counts
  and keeps the per-snapshot report rows as `reported_rows` for reconciliation.
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

## Full split inventory (truth from parquet)

`compressed_bytes` is the size of the gzipped SQLite snapshot in the operator's
Bunny CDN archive (informational only — the parquet on HF is what consumers
use). `rows` and `parts` are summed directly from the published parquet files.

| snapshot_id | compressed_bytes | rows | parts | status |
|---|---:|---:|---:|---|
| `polymarket_btc_data_2026-03-14_193215` | 10,935,294,993 | 236,166,002 | 251 | ok |
| `polymarket_btc_data_2026-03-29_215354` | 10,069,965,097 | 193,440,294 | 187 | partial |
| `polymarket_btc_data_2026-03-22_215354` |  9,691,222,331 |   6,630,470 | 159 | partial |
| `polymarket_btc_data_2026-04-10_232833` |  7,124,484,517 |   7,850,934 |  49 | ok |
| `polymarket_btc_data_2026-05-14_205654` |    393,243,254 |   7,548,862 |  12 | ok |
| `polymarket_btc_data_2026-04-03_232930` |    176,080,358 |   3,330,628 |  10 | ok |
| `polymarket_btc_data_2026-05-13_183517` |    260,463,524 |     433,328 |   8 | partial |
| `polymarket_btc_data_2026-05-14_003913` |     18,516,286 |     400,710 |  11 | ok |
| `polymarket_btc_data_2026-05-14_085654` |    241,952,456 |     199,313 |  11 | ok |
| `polymarket_btc_data_2026-04-21_211838` |  7,200,674,285 |      25,746 |  40 | partial |

`polymarket_btc_data_2026-04-21_211838` was partially exported — its SQLite
image is heavily corrupted and DuckDB couldn't attach it; only the rows that
the sqlite3 fallback could recover made it into the published parquet.

The three partial snapshots (`2026-03-29_215354`, `2026-03-22_215354`,
`2026-05-13_183517`) are partial because some of their tables had corrupt
B-tree pages that aborted the DuckDB bulk-import. The sqlite3 fallback then
walked the remaining pages table-by-table. The `lag_pairs_ms` and ticks tables
in those snapshots are typically the ones that didn't fully survive.

## Per-table coverage (full/) — truth from parquet

| table | rows | parts |
|---|---:|---:|
| `binance_trades` | 62,516,222 | 56 |
| `binance_ticks_ms` | 42,659,034 | 37 |
| `polymarket_ticks_ms` | 347,279,235 | 37 |
| `lag_pairs_ms` | 3,181,298 | 11 |
| `binance_candles_1s` | 263,151 | 32 |
| `binance_candles_5s` | 91,308 | 68 |
| `binance_candles_1m` | 18,359 | 126 |
| `binance_candles_5m` | 5,008 | 126 |
| `binance_candles_15m` | 2,121 | 124 |
| `binance_candles_1h` | 838 | 111 |
| `market_meta` | 9,713 | 10 |
| `crossover_alerts` | 0 | 0 |

The aggregate JSON also keeps each table's `reported_rows` (from per-snapshot
export reports) so consumers can see how much the partial re-exports lost.
Typical gaps are 0–2 % on partial snapshots; full snapshots reconcile
exactly.

## Validation (post-upload, from a clean download)

```
split:           full
snapshots:       10
truth rows:      456,026,287      (sum of parquet num_rows)
reported rows:   456,966,949      (sum of per-snapshot report rows)
delta:           -940,662         (partial exports over-counted)
files:           738              (matches local)
bytes:           3,472,100,738    (matches local)
file integrity:  OK
row integrity:   WARN (partial exports over-count by ~0.2 %)
```

The `delta` is the gap between the per-snapshot reports (which the v2 exporter
wrote as rows-walked) and the parquet files on disk (which lost some rows to
SQLite page corruption during the partial-export fallback). The parquet files
are the truth source.

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
- Re-export partial snapshots against fresh SQLite copies if/when the
  underlying recording is rerun.
- Merge / dedupe / time-align across the 10 published snapshots (each
  snapshot covers overlapping date ranges).
- Benchmark the OpenMarket Rust backtester against this published split.
- Roll forward to v0.3 with the next 10 snapshots.

## Notes

This release is the **research-platform release**. All data on HF is from the
public Bunny CDN, no live trading endpoints are touched, and no production
credentials are exposed.