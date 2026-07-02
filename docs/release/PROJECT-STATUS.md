# OpenMarket Project Status

Last updated: 2026-07-02

## Status

OpenMarket is in archival shutdown. The full CDN archive is published on
Hugging Face; GitHub source remains **private** until public launch.

Active data collection and active strategy development are over. The clean-lane
snapshot publication pass completed on 2026-07-01: all 202 inventoried CDN
snapshots are exported locally and synced to the public Hugging Face `full/`
split. Unified backfill partitions synced as `v0.4.3-unified` on 2026-07-02.

## What Is Public Today

- Hugging Face dataset: `gregyoung14/openmarket-btc-polymarket`
  - `v0.1-sample` — 12 flat parquet at repo root (9,352 rows)
  - `full/` (`v0.2-full`) — complete 202-snapshot CDN archive (3,312 parquet
    files; re-uploaded 2026-07-01 after sqlite3 recovery)
  - `unified/` (`v0.4.3-unified`) deduped timeline (~727M rows, 504 parquet
    files; backfill partitions for 2026-03-23 and 2026-05-15)
- Hugging Face models: `gregyoung14/openmarket-models`
  - `v0.2.1/` calibrated binary-outcome model (357k rows, unified Parquet step3)
  - `v0.2/` prior release (354k rows)
  - `v0.1/` earlier release (historical)

## What Is Private

- GitHub source: `github.com/gregyoung14/openmarket` (**private**)
  - Rust ML pipeline (`step3-parquet-export`, `binary-outcome-trainer`,
    `unified-backfill`), docs, paper, and release tooling live here
  - Source tag `v0.5.1` tracks the backfill + Rust trainer release
  - `v0.5.0` remains the archival milestone tag

## Archive Coverage

- Total archived CDN snapshots inventoried in
  `data/hf_release/metadata/snapshot_manifest.json`: 202
- Snapshots with local export reports: 202
- Snapshots reflected in the published queue metadata:
  - `published-clean`: 202
  - `published-partial`: 0
  - `corrupt`: 0

The public `full/` split matches the complete fixed CDN inventory.

### Unified backfill limits

| Gap | Notes |
|---|---|
| Apr 22 – May 12 | No snapshots in archive — empty timeline |
| ~1,241 markets | `no_poly_ticks` — never collected |
| ~922 markets | `insufficient_trades` — too sparse for step3 |
| Step3 coverage | 2,251 / 4,450 `market_meta` markets (51%) |

## Remaining Archive-Closeout Work

1. ~~Unified backfill sync to HF (`v0.4.3-unified`)~~ (done 2026-07-02).
2. ~~Rust ML pipeline + doc version sync~~ (done 2026-07-02).
3. **Deferred:** flip GitHub repository to public when launch-ready.
4. **Optional (not required):** full-archive `features/` HF upload — reproducible
   from `unified/` via Rust exporters.
5. **Optional:** compile and submit arXiv bundle (`paper/scripts/export-arxiv.sh`).

## What This Project Is Not Doing Anymore

- No new live data collection
- No rolling snapshot ingestion beyond the existing CDN archive
- No claim of ongoing model maintenance
- No claim of deployable production trading alpha

## Decision Rule For Final Closure

The archived CDN inventory has been published in full, `unified/` has been
backfilled and synced to Hugging Face, and documentation is reconciled for
source tag `v0.5.1`. The public research record on HF is complete; GitHub
remains private until launch.