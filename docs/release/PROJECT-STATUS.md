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
  - `sample/` (`v0.1-sample`)
  - `full/` (`v0.2-full`) — complete 202-snapshot CDN archive (3258 parquet
    files as of 2026-07-01)
  - `unified/` (`v0.4-unified`) deduped from the complete 202-snapshot archive
    (586M rows, 467 parquet files, synced to HF 2026-07-01)
- Hugging Face models: `gregyoung14/openmarket-models`
  - `v0.1/` calibrated binary-outcome model payload and metrics

## Archive Coverage

- Total archived CDN snapshots inventoried in
  `data/hf_release/metadata/snapshot_manifest.json`: 202
- Snapshots with local export reports: 202
- Snapshots reflected in the published queue metadata:
  - `published-clean`: 198
  - `published-partial`: 4
  - `corrupt`: 0

The public `full/` split now matches the complete fixed CDN inventory.

## Remaining Archive-Closeout Work

1. Push source tag `v0.5.0` and verify GitHub/HF release workflow alignment.
2. Optional: refresh LaTeX/arXiv bundle if the paper is submitted externally.

## What This Project Is Not Doing Anymore

- No new live data collection
- No rolling snapshot ingestion beyond the existing CDN archive
- No claim of ongoing model maintenance
- No claim of deployable production trading alpha

## Decision Rule For Final Closure

The archived CDN inventory has been published in full, the `unified/` split has
been rebuilt from it, and documentation has been reconciled for source tag
`v0.5.0`. The project is a complete public research archive.