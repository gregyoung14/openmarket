# OpenMarket v0.1.0 — first public research platform release

OpenMarket is a high-frequency prediction-market research platform pairing
Binance BTC/USDT market data with Polymarket BTC binary-order-book events.

## What's in v0.1.0

- **Rust workspace** (10 crates): `exchange-binance`, `exchange-polymarket`,
  `signal-engine`, `execution-engine`, `recorder`, `paper-executor`,
  `data-prep`, `dataset-downloader`, `backtester`, `common`.
- **Python services**: redemption reconciliation, paper-tournament dashboard,
  dataset tooling (`scripts/datasets/`), Hugging Face release automation
  (`scripts/hf/`).
- **Hugging Face Hub artifacts**:
  - Dataset sample split: [`gregyoung14/openmarket-btc-polymarket`](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)
  - Model card scaffold: [`gregyoung14/openmarket-models`](https://huggingface.co/gregyoung14/openmarket-models)

## Verification

- `cargo check --workspace`: 0 errors, 16 dead-code warnings
- HF sample round-trip: 12/12 tables match expected row counts
- Benchmark baseline: 9,352 rows, 204 KB parquet, 1.55s download

## Pre-release checklist

- [x] Push `gregyoung14/openmarket`
- [x] Create `gregyoung14/openmarket-btc-polymarket`
- [x] Create `gregyoung14/openmarket-models`
- [x] Upload dataset card + sample split + manifests
- [x] Validate sample split from a clean download
- [x] `cargo check --workspace`
- [x] `python3 -m py_compile scripts/datasets/*.py scripts/hf/*.py`
- [x] Record benchmark baseline
- [x] Decide pretrained-model policy (deferred)

## Release artifacts

```text
Source tag:      v0.1.0
Source repo:     github.com/gregyoung14/openmarket
Dataset:         huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version: v0.1-sample
Models:          huggingface.co/gregyoung14/openmarket-models
Model version:   deferred; model-card scaffold uploaded
Paper:           paper/paper.md
```

## Post-release follow-ups (issues to open)

- Full Parquet export of all five large snapshots (≈45 GB compressed)
- Merge / dedupe / validation pipeline across snapshots
- First benchmark table comparing signal versions
- Example notebooks (HF Spaces-compatible)
- Pretrained v0.1 model trained on the full dataset

## Notes

This release is the research-platform scaffolding. The dataset currently
shipped to Hugging Face is the smallest validated SQLite snapshot from the
operator's archive (`polymarket_btc_data_2026-05-14_145928.db.gz`, 294 KB
compressed) so anyone can run the pipeline end-to-end without downloading
the full multi-gigabyte archive.

The full historical archive (202 snapshots, ~46 GB compressed) is inventoried
in `metadata/snapshot_manifest.{json,tsv}` inside the dataset repo. Future
releases will publish the larger snapshots under a `full/` split.