# Datasets

Large datasets are released outside Git.

Planned Hugging Face dataset:

```text
gregyoung14/openmarket-btc-polymarket
```

## Partitions

```text
raw/
  binance_ticks/
  polymarket_books/
processed/
  aligned/
  features/
  labels/
metadata/
  markets/
  schemas/
  checksums/
```

## Initial Source Snapshot

The current source archive is a SQLite snapshot series produced by the recorder.
The first large snapshot covers approximately February 12 through March 11,
2026, and contains Binance trades, Binance millisecond ticks, Polymarket
millisecond order book events, market metadata, candle tables, and lag-pair
records.

The public dataset release should convert this into versioned Parquet partitions
with checksums and schema files.

## Do Not Commit

- SQLite databases
- Parquet partitions
- CSV exports
- HTML reports
- model binaries

Use `datasets/download.py` to fetch artifacts into `data/`.
