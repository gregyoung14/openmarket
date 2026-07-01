# Dataset Release Plan

## Artifact Stores

Primary:

```text
huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
```

Secondary archival options:

- Cloudflare R2
- Backblaze B2
- Academic Torrents for very large immutable releases

## Versioning

Every dataset release should include:

- semantic dataset version, for example `openmarket-btc-v0.1`
- date range
- source code commit
- schema hash
- row counts
- checksums
- known gaps
- license and terms notes

## Target Layout

```text
raw/
  binance_ticks/date=YYYY-MM-DD/*.parquet
  polymarket_books/date=YYYY-MM-DD/*.parquet
processed/
  aligned/date=YYYY-MM-DD/*.parquet
  features/version=v0.1/date=YYYY-MM-DD/*.parquet
  labels/version=v0.1/date=YYYY-MM-DD/*.parquet
metadata/
  markets/*.parquet
  schemas/*.json
  checksums/*.json
```

## Initial Migration Steps

1. Export SQLite tables to typed Parquet partitions.
2. Write schema JSON for every partition family.
3. Compute row counts and checksums.
4. Publish a small sample split first.
5. Publish the full dataset after sample validation.
6. Add the Hugging Face dataset card from `datasets/dataset-card.md`.
