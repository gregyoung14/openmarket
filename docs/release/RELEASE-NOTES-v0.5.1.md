# OpenMarket v0.5.1 — Unified backfill sync

Post-`v0.5.0` maintenance release. Hugging Face dataset and model artifacts
updated; GitHub source is public.

## Dataset (`v0.4.3-unified`)

Adds five `unified/` date partitions missing from `v0.4.2-unified`:

| Partition | Rows (approx.) | Source |
|---|---:|---|
| `binance_trades/date=2026-03-23` | 191,528 | `2026-03-29` staging SQLite |
| `binance_trades/date=2026-05-15` | 558 | `2026-06-23` staging SQLite |
| `binance_ticks_ms/date=2026-05-15` | 391 | same |
| `polymarket_ticks_ms/date=2026-05-15` | 8,500 | same |
| `lag_pairs_ms/date=2026-05-15` | 4,840 | same |

Provenance: `unified/metadata/sqlite_fill.json` on HF.

**Unrecoverable gaps (unchanged):** April 22–May 12 collection hole; ~1,241
markets with no Polymarket ticks; ~49% of `market_meta` lacks sufficient data
for step3 export.

## Models (`v0.2.1`)

Retrained on backfilled unified Parquet via Rust pipeline:

- `export_step3_from_parquet` → 357,390 rows, 2,251 markets
- `train_binary_outcome_model` → AUC 0.838, Brier 0.165, ECE 0.025

Supersedes `v0.2/` as the recommended release.

## Tooling

- `crates/unified-backfill` — scan, repair, sqlite-fill
- `crates/step3-parquet-export` — Parquet-native step3 export + `--audit`
- `crates/binary-outcome-trainer` — walk-forward LR + Platt (~67s train)

Python training scripts removed; `scripts/ml/README.md` documents Rust path.

## GitHub visibility

Source tag `v0.5.1` is pushed to the public `gregyoung14/openmarket`
repository. HF dataset and model repos remain public.
