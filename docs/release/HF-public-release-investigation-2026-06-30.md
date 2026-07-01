# Hugging Face Public Dataset Release Investigation

Date: 2026-06-30

## Current Storage Model

The canonical live database is SQLite:

- Production DB: `/mnt/nvme/polymarket_btc_data.db`
- Backup service: `rust-services/db-backup`
- Backup API: `POST http://<server>:8007/backup`
- Health API: `GET http://<server>:8007/health`
- Public CDN path pattern: `https://glitchrun-xyz.b-cdn.net/polymarket-bot/polymarket_btc_data_<YYYY-MM-DD>_<HHMMSS>.db.gz`
- Bunny Storage target in code: storage zone `glitchrun-xyz`, folder `polymarket-bot`

The live systemd unit overrides the Rust defaults and currently describes the intended offload model:

- `DB_BACKUP_INTERVAL_SECS=21600` (6 hours)
- `DB_PRUNE_INTERVAL_SECS=21600` (6 hours)
- `DB_PRUNE_RETENTION_DAYS=0`
- `DB_PRUNE_REQUIRED_RECENT_BACKUP_MAX_AGE_SECS=43200`

The Rust defaults are different (`7 days` backup interval, `14 days` prune retention), so live release tooling must read the actual service health/config or systemd environment instead of trusting defaults.

Public directory listing is disabled on the CDN. The known March snapshot is reachable directly and is about 10.9 GB compressed:

- `polymarket_btc_data_2026-03-14_193215.db.gz`

The Bunny Storage API is required to enumerate every snapshot unless we create and maintain a separate manifest file.

## Bunny Snapshot Inventory

Inventory was run from the local machine against the VPS on 2026-07-01 using
the Bunny access key from the `db-backup.service` user unit. The generated
manifest was written on the VPS to:

```text
/mnt/nvme/code/polymarket-btc-scraper/data/hf_release/metadata/snapshot_manifest.json
/mnt/nvme/code/polymarket-btc-scraper/data/hf_release/metadata/snapshot_manifest.tsv
```

The local OpenMarket checkout also has a copy under ignored `data/hf_release/`
for release processing.

Inventory summary:

| Metric | Value |
|---|---:|
| Snapshot count | 202 |
| Total compressed bytes | 46,205,325,113 |
| Total compressed GB | 46.205 |
| First snapshot | `polymarket_btc_data_2026-03-14_193215.db.gz` |
| Last snapshot | `polymarket_btc_data_2026-07-01_025654.db.gz` |
| Snapshots >= 1 GB | 5 |
| Bytes in snapshots >= 1 GB | 45.022 GB |
| Snapshots 10 MB to 1 GB | 5 |
| Bytes in snapshots 10 MB to 1 GB | 1.090 GB |
| Snapshots < 10 MB | 192 |
| Bytes in snapshots < 10 MB | 0.093 GB |

Largest snapshots:

| Snapshot | Compressed bytes |
|---|---:|
| `polymarket_btc_data_2026-03-14_193215.db.gz` | 10,935,294,993 |
| `polymarket_btc_data_2026-03-29_215354.db.gz` | 10,069,965,097 |
| `polymarket_btc_data_2026-03-22_215354.db.gz` | 9,691,222,331 |
| `polymarket_btc_data_2026-04-21_211838.db.gz` | 7,200,674,285 |
| `polymarket_btc_data_2026-04-10_232833.db.gz` | 7,124,484,517 |

The inventory strongly suggests the first HF processing pass should prioritize
the five large snapshots, then treat the numerous ~485 KB snapshots as a
separate validation problem because they may only contain schema, metadata, or
post-prune residue.

## Data In The SQLite Snapshots

High-frequency raw tables:

- `binance_trades`: raw Binance BTC/USDT trades.
- `binance_ticks_ms`: normalized Binance websocket ticks.
- `polymarket_ticks_ms`: normalized Polymarket book/trade/last-price events.
- `lag_pairs_ms`: precomputed Binance/Polymarket nearest-tick pairings.

Aggregated tables:

- `binance_candles_1s`
- `binance_candles_5s`
- `binance_candles_1m`
- `binance_candles_5m`
- `binance_candles_15m`
- `binance_candles_1h`

Metadata:

- `market_meta`
- `crossover_alerts`

Operational/trading data also exists outside the SQLite archive:

- `data/trade_ledger.json`
- `data/ml_artifacts/*.json`

Those should not be mixed into the raw market-data release without a separate privacy/risk review.

## Existing Export Paths

The market recorder already has local export endpoints:

- `GET /export/step1`: lag pair CSV.
- `GET /export/step2`: 15-minute feature CSV.
- `GET /export/step2_hf`: 100 ms and 1 s feature CSVs for the last 72 hours of the local DB.
- `GET /export/step3_binary_calibration`: binary outcome calibration dataset with manifest.

These are useful references, but they are not enough for the full public release because the local DB is intentionally pruned after CDN offload. A complete release needs an archive processor that works across all CDN snapshots.

## Release Strategy

Recommended Hugging Face layout:

```text
data/
  raw/
    binance_trades/date=YYYY-MM-DD/part-NNNN.parquet
    binance_ticks_ms/date=YYYY-MM-DD/part-NNNN.parquet
    polymarket_ticks_ms/date=YYYY-MM-DD/part-NNNN.parquet
    lag_pairs_ms/date=YYYY-MM-DD/part-NNNN.parquet
  candles/
    interval=1s/date=YYYY-MM-DD/part-NNNN.parquet
    interval=5s/date=YYYY-MM-DD/part-NNNN.parquet
    interval=1m/date=YYYY-MM-DD/part-NNNN.parquet
    interval=5m/date=YYYY-MM-DD/part-NNNN.parquet
    interval=15m/date=YYYY-MM-DD/part-NNNN.parquet
    interval=1h/date=YYYY-MM-DD/part-NNNN.parquet
  features/
    step2_1s/date=YYYY-MM-DD/part-NNNN.parquet
    step2_100ms/date=YYYY-MM-DD/part-NNNN.parquet
    step3_binary_calibration/date=YYYY-MM-DD/part-NNNN.parquet
metadata/
  snapshot_manifest.parquet
  schema.json
  quality_report.json
README.md
```

Parquet is the best default for Hugging Face compatibility, compression, typed schemas, and dataset viewer support. Keep raw SQLite snapshots out of the primary dataset repo unless published as optional archival artifacts; they are too coarse and hard to load directly.

## Cleanup Requirements

For each snapshot:

1. Download/decompress to a staging volume.
2. Run `PRAGMA integrity_check`.
3. Record source filename, compressed size, uncompressed size, modified time, and table ranges.
4. Export tables to typed Parquet.
5. Drop or normalize `raw_json` columns into separate optional files if the main release needs to stay compact.
6. Partition by UTC date from the table's event timestamp.
7. Dedupe across overlapping snapshots.
8. Validate monotonic timestamp ranges and row counts.
9. Emit per-table stats and anomaly reports.
10. Remove staged SQLite before processing the next snapshot.

Suggested dedupe keys:

- `binance_trades`: `trade_id`.
- `binance_ticks_ms`: `(source_ts_ms, trade_time_ms, price, volume)`.
- `polymarket_ticks_ms`: `(source_ts_ms, market_slug, asset_id, side_label, event_type, price, best_bid, best_ask, size)`.
- `lag_pairs_ms`: `(paired_at_ms, market_slug, side_label, binance_source_ts_ms, polymarket_source_ts_ms, polymarket_bid)`.
- Candles: `candle_start`.
- `market_meta`: `market_slug`, keeping latest `last_seen_ms`.

## Privacy And Risk Review

Likely safe for public raw market release:

- Public Binance market data.
- Public Polymarket order book/trade event data.
- Market metadata.
- Derived lag/features/calibration rows.

Hold back or review separately:

- `data/trade_ledger.json`, because it may reveal strategy execution history.
- Any wallet addresses, private execution metadata, bankroll data, or internal signal versions.
- Raw JSON payloads, until sampled for accidental tokens or service-specific metadata.
- Model artifacts, unless the release includes a clear model-card style explanation.

## Immediate Next Steps

1. Use the generated `data/hf_release/metadata/snapshot_manifest.json` as the
   source of truth for archive processing.
2. Use `scripts/datasets/export_snapshot_to_parquet.py` to convert one
   `.db.gz` snapshot into partitioned Parquet.
3. Build `scripts/datasets/merge_partitions.py` to dedupe and compact partition files across snapshots.
4. Build `scripts/datasets/validate_release.py` to check row counts, timestamp coverage, duplicate rates, null rates, and schema consistency.
5. Create a Hugging Face `README.md` dataset card with provenance, schema, timestamp semantics, known gaps, and intended use.

Inventory command for the VPS:

```bash
export BUNNY_CDN_ACCESS_KEY="$(
  systemctl --user cat db-backup.service |
    sed -n 's/^Environment=BUNNY_CDN_ACCESS_KEY=//p' |
    head -1
)"

scripts/datasets/inventory_bunny_snapshots.py --print-tsv
```

Single-snapshot Parquet export:

```bash
python3 -m pip install -r scripts/datasets/requirements.txt

scripts/datasets/export_snapshot_to_parquet.py \
  polymarket_btc_data_2026-03-14_193215.db.gz \
  --manifest data/hf_release/metadata/snapshot_manifest.json \
  --out-dir data/hf_release/parquet
```
