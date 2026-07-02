# OpenMarket Project Status

Last updated: 2026-07-01

## Status

OpenMarket is in archival shutdown. The full CDN archive is now published.

Active data collection and active strategy development are over. The clean-lane
snapshot publication pass completed on 2026-07-01: all 202 inventoried CDN
snapshots are exported locally and synced to the public Hugging Face `full/`
split.

## What Is Public Today

- Source code and documentation: `github.com/gregyoung14/openmarket`
- Hugging Face dataset: `gregyoung14/openmarket-btc-polymarket`
  - `v0.1-sample` — 12 flat parquet at repo root (9,352 rows)
  - `full/` (`v0.2-full`) — complete 202-snapshot CDN archive (3,312 parquet
    files; re-uploaded 2026-07-01 after sqlite3 recovery)
  - `unified/` (`v0.4.2-unified`) deduped from the complete 202-snapshot archive
    (~722M rows, 499 parquet files; refreshed on HF 2026-07-01)
- Hugging Face models: `gregyoung14/openmarket-models`
  - `v0.2/` calibrated binary-outcome model (354k rows, unified Parquet step3)
  - `v0.1/` earlier release (historical)

## Archive Coverage

- Total archived CDN snapshots inventoried in
  `data/hf_release/metadata/snapshot_manifest.json`: 202
- Snapshots with local export reports: 202
- Snapshots reflected in the published queue metadata:
  - `published-clean`: 202
  - `published-partial`: 0
  - `corrupt`: 0

The public `full/` split now matches the complete fixed CDN inventory.

## Remaining Archive-Closeout Work

1. ~~Documentation polish on GitHub and Hugging Face~~ (done 2026-07-01).
2. **Optional (not required):** full-archive `features/` HF upload. The archive
   is complete without it — step2/step3 features are reproducible from `unified/`
   via `scripts/ml/` and the Rust exporters (`step3-parquet-export`,
   `binary-outcome-trainer`). HF currently ships a one-snapshot `features/`
   demo (`v0.4-features`, 2 parquet files) for schema reference only.
3. Optional: compile and submit arXiv bundle (\texttt{paper/scripts/export-arxiv.sh}).
4. Optional: tag `v0.5.1` on `main` for post-recovery doc commits; `v0.5.0`
   remains the archival milestone tag.

## What This Project Is Not Doing Anymore

- No new live data collection
- No rolling snapshot ingestion beyond the existing CDN archive
- No claim of ongoing model maintenance
- No claim of deployable production trading alpha

## Decision Rule For Final Closure

The archived CDN inventory has been published in full, the `unified/` split has
been rebuilt from it, and documentation has been reconciled for source tag
`v0.5.0`. The project is a complete public research archive.