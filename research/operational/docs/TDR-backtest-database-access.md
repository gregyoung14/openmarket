# TDR: Backtest Database Access via CDN

## Overview

Full SQLite database snapshots are uploaded to Bunny CDN every 6 hours by the `db-backup` service. After each successful upload, the VPS prunes tick-level data older than 0 days and VACUUMs the database to reclaim disk space.

Each CDN snapshot is a **self-contained, immutable point-in-time database** for the most recent live capture window. There is no S3; Bunny CDN is the sole archive.

## Snapshot Model

```
VPS (live)                          Bunny CDN (archive)
┌──────────────┐                    ┌──────────────────────────────────────┐
│ /var/lib/polymarket/   │  ── backup ──►    │ polymarket_btc_data_2026-05-13_000000.db.gz │ recent 6h window
│ short local  │                    │ polymarket_btc_data_2026-05-13_060000.db.gz │ next 6h window
│ capture only │  ── prune ──►     │ polymarket_btc_data_2026-05-13_120000.db.gz │ next 6h window
│              │  (post-upload)     │ ...                                        │
└──────────────┘                    └──────────────────────────────────────┘
```

**Key properties:**
- Each snapshot is frozen and complete — no dependencies on other snapshots
- Local disk stays bounded by aggressively pruning the live SQLite after each offload
- Snapshots are emitted every 6 hours; stitch adjacent snapshots together for longer-range backtests
- CDN storage is cheap (~$0.01/GB/month at Bunny)

### What gets pruned locally (after successful CDN upload)

| Table | Timestamp Column | Retention |
|-------|-----------------|-----------|
| `polymarket_ticks_ms` | `source_ts_ms` | 0 days |
| `binance_ticks_ms` | `source_ts_ms` | 0 days |
| `binance_trades` | `trade_time` | 0 days |
| `lag_pairs_ms` | `paired_at_ms` | 0 days |
| `binance_candles_1s` | `candle_start` | 0 days |
| `binance_candles_5s` | `candle_start` | 0 days |

**Never pruned** (small tables): `binance_candles_1m`, `binance_candles_5m`, `binance_candles_15m`, `binance_candles_1h`, `market_meta`, `crossover_alerts`

## Available Snapshots

```
https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_<DATE>.db.gz
```

| Snapshot | Date Range | Compressed | Notes |
|----------|-----------|------------|-------|
| `polymarket_btc_data_2026-03-14_193215.db.gz` | Feb 12 → Mar 11 | ~10.2 GB | Full history (pre-prune) |
| *(future snapshots auto-added every 6 hours)* | recent capture window | varies | Post-prune snapshots |

- **Public access:** No auth required — direct HTTP GET
- **First snapshot is special:** Contains all historical data before pruning was enabled

## Quick Start

### 1. Download & Decompress

```bash
# Stream download + decompress in one pass (no double disk usage)
curl -L "https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz" \
  | gunzip > polymarket_btc_data.db

# Or download compressed first, then decompress
curl -LO "https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz"
gunzip polymarket_btc_data_2026-03-14_193215.db.gz
```

> **Disk requirement:** ~100 GB free for the uncompressed database. Use an SSD — spinning disk will be unusably slow for queries on this dataset.

### 2. Verify Integrity

```bash
sqlite3 polymarket_btc_data.db "PRAGMA integrity_check;" 
# Expected: ok

sqlite3 polymarket_btc_data.db "SELECT count(*) FROM binance_trades;"
# Expected: ~39,800,000
```

### 3. Query from Python

```python
import sqlite3
import pandas as pd

DB_PATH = "polymarket_btc_data.db"

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

# Example: Load 1-minute Binance candles
candles = pd.read_sql_query("""
    SELECT 
        candle_start,
        datetime(candle_start/1000, 'unixepoch') as dt,
        open_price, high_price, low_price, close_price,
        volume, trade_count
    FROM binance_candles_1m
    ORDER BY candle_start
""", conn)

# Example: Load Polymarket ticks for a specific market
poly_ticks = pd.read_sql_query("""
    SELECT 
        source_ts_ms,
        datetime(source_ts_ms/1000, 'unixepoch') as dt,
        market_slug, side_label, event_type,
        price, best_bid, best_ask, size
    FROM polymarket_ticks_ms
    WHERE market_slug = 'btc-updown-15m-1773175500'
    ORDER BY source_ts_ms
""", conn)

# Example: Matched lead-lag pairs
pairs = pd.read_sql_query("""
    SELECT 
        paired_at_ms,
        datetime(paired_at_ms/1000, 'unixepoch') as dt,
        market_slug, side_label,
        lead_lag_ms, binance_price, polymarket_bid,
        price_delta_bps, quality_flag
    FROM lag_pairs_ms
    ORDER BY paired_at_ms
""", conn)

conn.close()
```

## Schema Reference

### Tick-Level Data (High-Frequency)

#### `binance_trades` — ~39.8M rows
Raw Binance BTC/USDT trade stream.

| Column | Type | Description |
|--------|------|-------------|
| `trade_id` | INTEGER PK | Binance trade ID |
| `trade_time` | INTEGER | Trade timestamp (ms since epoch) |
| `price` | REAL | Trade price |
| `quantity` | REAL | BTC quantity |
| `quote_volume` | REAL | USDT volume |
| `is_buyer_maker` | INTEGER | 1 = sell (taker sold), 0 = buy |
| `received_at` | INTEGER | Server receive timestamp (ms) |

**Index:** `idx_binance_trades_time (trade_time DESC)`

#### `binance_ticks_ms` — ~34.9M rows
Binance WebSocket tick snapshots (millisecond resolution).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `source_ts_ms` | INTEGER | Exchange timestamp (ms) |
| `ingest_ts_ms` | INTEGER | Server ingest timestamp (ms) |
| `trade_time_ms` | INTEGER | Trade time from exchange (ms) |
| `price` | REAL | BTC price |
| `volume` | REAL | Trade volume |
| `raw_json` | TEXT | Full WebSocket message JSON |

**Indexes:** `source_ts_ms`, `ingest_ts_ms`

#### `polymarket_ticks_ms` — ~159M rows
Polymarket WebSocket events (book updates, trades, last-trade-price).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `source_ts_ms` | INTEGER | Event timestamp (ms) |
| `ingest_ts_ms` | INTEGER | Server ingest timestamp (ms) |
| `market_slug` | TEXT | Market identifier, e.g. `btc-updown-15m-1773175500` |
| `asset_id` | TEXT | Polymarket token/condition ID |
| `side_label` | TEXT | `Up` or `Down` |
| `event_type` | TEXT | `book`, `trade`, `last_trade_price` |
| `price` | REAL | Trade/last price (nullable) |
| `best_bid` | REAL | Top-of-book bid |
| `best_ask` | REAL | Top-of-book ask |
| `size` | REAL | Trade size (nullable) |
| `paired` | INTEGER | 1 = matched to a Binance tick in lag_pairs_ms |
| `raw_json` | TEXT | Full WebSocket message JSON |

**Indexes:** `source_ts_ms`, `ingest_ts_ms`, `asset_id`, `(paired, source_ts_ms)`

### Pre-Computed Pairs

#### `lag_pairs_ms` — ~1.7M rows
Matched Binance↔Polymarket tick pairs with lead-lag measurement.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `paired_at_ms` | INTEGER | Pairing timestamp (ms) |
| `market_slug` | TEXT | Market identifier |
| `side_label` | TEXT | `Up` or `Down` |
| `binance_tick_id` | INTEGER | FK → `binance_ticks_ms.id` |
| `polymarket_tick_id` | INTEGER | FK → `polymarket_ticks_ms.id` |
| `binance_source_ts_ms` | INTEGER | Binance tick time (ms) |
| `polymarket_source_ts_ms` | INTEGER | Polymarket tick time (ms) |
| `lead_lag_ms` | INTEGER | `polymarket - binance` (positive = Polymarket lags) |
| `binance_price` | REAL | BTC price at Binance tick |
| `polymarket_bid` | REAL | Polymarket best bid at match time |
| `price_delta_bps` | REAL | Price divergence in basis points |
| `quality_flag` | TEXT | Pairing quality indicator |

**Indexes:** `paired_at_ms`, `lead_lag_ms`

### Candle Data (Aggregated)

Identical schema across all timeframes: `binance_candles_1s`, `binance_candles_5s`, `binance_candles_1m`, `binance_candles_5m`, `binance_candles_15m`, `binance_candles_1h`

| Column | Type | Description |
|--------|------|-------------|
| `candle_start` | INTEGER PK | Candle open timestamp (ms since epoch) |
| `candle_end` | INTEGER | Candle close timestamp (ms) |
| `open_price` | REAL | Open |
| `high_price` | REAL | High |
| `low_price` | REAL | Low |
| `close_price` | REAL | Close |
| `volume` | REAL | BTC volume |
| `quote_volume` | REAL | USDT volume |
| `trade_count` | INTEGER | Number of trades in candle |
| `created_at` | INTEGER | Server-side creation timestamp (ms) |

### Metadata

#### `market_meta`
Polymarket market registry.

| Column | Type | Description |
|--------|------|-------------|
| `market_slug` | TEXT PK | e.g. `btc-updown-15m-1773175500` |
| `question` | TEXT | Market question text |
| `up_token_id` | TEXT | Up outcome token/condition ID |
| `down_token_id` | TEXT | Down outcome token/condition ID |
| `up_price` | REAL | Last known Up price |
| `down_price` | REAL | Last known Down price |
| `first_seen_ms` | INTEGER | First appearance timestamp (ms) |
| `last_seen_ms` | INTEGER | Last update timestamp (ms) |

#### `crossover_alerts`
Signal crossover events (MA crossovers used for trade signals).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `alert_type` | TEXT | Alert category |
| `timestamp` | INTEGER | Alert timestamp |
| `trade_time` | INTEGER | Associated trade time |
| `price` | REAL | Price at crossover |
| `ma_25` | REAL | 25-period moving average |
| `signal` | TEXT | Signal direction |
| `created_at` | TIMESTAMP | Row creation time |

## Timestamps

**All timestamps are milliseconds since Unix epoch (1970-01-01 UTC).**

```python
# Convert to datetime
from datetime import datetime, timezone

ts_ms = 1739318400000
dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
# → 2026-02-12 00:00:00+00:00

# In SQLite
# datetime(ts_ms/1000, 'unixepoch') → '2026-02-12 00:00:00'
```

## Performance Tips

1. **Use `mode=ro`** for read-only access — prevents WAL creation and accidental writes.
2. **For tick-level queries**, always filter by `source_ts_ms` range first — the indexes are on timestamps.
3. **The `raw_json` columns are large** — exclude them from SELECT unless needed. This single optimization can 10x query speed.
4. **For backtesting candle-based strategies**, use the candle tables directly — they're orders of magnitude smaller than tick data.
5. **Consider extracting a subset** if you only need a date range:

```bash
# Extract one day of 1-minute candles to a smaller DB
sqlite3 polymarket_btc_data.db "
  ATTACH 'backtest_subset.db' AS sub;
  CREATE TABLE sub.candles_1m AS 
    SELECT * FROM binance_candles_1m 
    WHERE candle_start >= 1739318400000 
      AND candle_start < 1739404800000;
"
```

6. **Memory-map for speed** on machines with enough RAM:

```python
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
conn.execute("PRAGMA mmap_size = 30000000000;")  # 30 GB mmap
```

## Backup & Prune Schedule

- **Backup frequency:** Every 7 days (automated via `db-backup` service on port 8007)
- **Post-backup prune:** Deletes tick data older than 14 days + VACUUM (automatic after each upload)
- **Naming:** `polymarket_btc_data_<YYYY-MM-DD>_<HHMMSS>.db.gz`
- **Trigger manual backup+prune:** `curl -X POST http://<server>:8007/backup`
- **Trigger prune only (no backup):** `curl -X POST http://<server>:8007/prune`
- **Check status:** `curl http://<server>:8007/health`

### Consuming Multiple Snapshots

Each snapshot is independent. To backtest across a wider date range than any single snapshot covers, query them separately:

```python
import sqlite3

# Old data (full history)
conn1 = sqlite3.connect("file:polymarket_btc_data_2026-03-14.db?mode=ro", uri=True)

# Recent data
conn2 = sqlite3.connect("file:polymarket_btc_data_2026-03-21.db?mode=ro", uri=True)

# Query each independently — don't try to merge, just union results in pandas
df1 = pd.read_sql_query("SELECT * FROM binance_candles_1m", conn1)
df2 = pd.read_sql_query("SELECT * FROM binance_candles_1m", conn2)
combined = pd.concat([df1, df2]).drop_duplicates(subset="candle_start").sort_values("candle_start")
```

## .gitignore Entry

```gitignore
# Polymarket backtest database (too large for git)
*.db
*.db.gz
polymarket_btc_data*
```
