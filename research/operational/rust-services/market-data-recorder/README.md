# Market Data Recorder

Rust service for millisecond-precision capture of Binance + Polymarket WebSocket events, lag pairing, and ML dataset export.

## What It Does

1. **Binance ingestion** — connects to `ws://127.0.0.1:8001/ws` (local Binance WS service), stores normalized tick data
2. **Polymarket multi-market ingestion** — connects directly to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribes to the next 16 markets (4 hours ahead), adds new markets every 14 minutes
3. **Lag pairing** — matches Binance and Polymarket ticks within ±750ms windows, computes lead/lag statistics
4. **ML dataset export** — generates 15-minute feature CSVs with 60+ technical, microstructure, and Polymarket features

## Port

**8005** (HTTP)

## API

| Endpoint | Description |
|---|---|
| `GET /health` | Connection status, subscriber count |
| `GET /stats` | Tick counts, lag pairing stats |
| `GET /warm-state` | Current warm state |
| `GET /export/step1` | Lag dataset CSV |
| `GET /export/step2` | 15-minute feature dataset CSV |

## Multi-Market Subscription

Unlike the single-market approach in v1, the recorder now subscribes to **all upcoming BTC markets** simultaneously:

- On WS connect: fetches the next 16 market windows (4 hours) from Gamma API, subscribes to all token IDs at once
- Every 14 minutes: adds any new upcoming markets not yet subscribed
- On WS reconnect: full re-subscribe happens automatically
- Token-to-market mapping is persisted in `market_meta` for correct side labeling

## Database

**Path:** `/var/lib/polymarket/polymarket_btc_data.db`

| Table | Description |
|---|---|
| `binance_ticks_ms` | Raw Binance trades with millisecond timestamps |
| `polymarket_ticks_ms` | Polymarket ticks (bid/ask per side per market, with `market_slug` and `side_label`) |
| `market_meta` | Market metadata (slug, question, UP/DOWN token IDs, prices, first/last seen) |
| `lag_pairs_ms` | Matched Binance-Polymarket tick pairs with lead/lag in ms, price delta in bps |

## Source Files

| File | Lines | Purpose |
|---|---|---|
| `ingest.rs` | ~500 | Multi-market WS subscription, Gamma API fetching, tick processing |
| `normalize.rs` | ~170 | Binance/Polymarket message parsing, side label resolution via token mapping |
| `lag.rs` | ~1,450 | Lag pairing algorithm + Step 1 (lag pairs) and Step 2 (15m features) CSV export |
| `db.rs` | ~400 | SQLite schema, batch inserts, query functions |
| `services.rs` | ~120 | Shared AppState, connection tracking, stat counters |
| `handlers.rs` | ~80 | HTTP endpoint handlers |
| `config.rs` | ~30 | Constants (ports, paths, URLs, intervals) |
| `models.rs` | ~50 | Data structures (BinanceTick, PolymarketTick, MarketMeta) |

## Export Directory

`data/ml_exports/`

## Running

```bash
systemctl --user restart market-data-recorder
systemctl --user status market-data-recorder
journalctl --user -u market-data-recorder -f
```

## Building

```bash
cd rust-services/market-data-recorder
cargo build --release
```
