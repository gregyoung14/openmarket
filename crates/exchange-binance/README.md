# Binance WebSocket Service (Rust)

Ultra-low latency real-time BTC price streaming service built in Rust.

## Architecture

This service follows Rust best practices with a modular structure:

```
src/
├── main.rs       # Entry point - wires up tasks and server
├── config.rs     # Configuration constants
├── models.rs     # Data structures (Trade, Candle)
├── services.rs   # Shared application state
├── tasks.rs      # Background async tasks
├── handlers.rs   # HTTP/WebSocket API handlers
└── db.rs         # Database operations
```

## Features

- **Real-time WebSocket streaming** from Binance
- **Batch DB writes** for optimal performance (100 trades/batch)
- **Multi-interval candle aggregation** (1s, 5s, 1m, 5m, 15m, 1h)
- **WebSocket broadcast** to connected clients (<50ms latency)
- **RESTful API** for historical data

## API Endpoints

- `GET /` - Health check
- `GET /health` - Detailed health status
- `GET /ws` - WebSocket connection for live updates
- `GET /candles/:interval?limit=N` - Historical candles

Valid intervals: `1s`, `5s`, `1m`, `5m`, `15m`, `1h`

## Performance Optimizations

- **SQLite WAL mode** with aggressive pragmas
- **Async I/O** using Tokio runtime
- **MPSC channels** for lock-free DB writes
- **Broadcast channels** for efficient WebSocket fanout
- **Blocking tasks** for sync DB operations
- **Zero-copy where possible** with efficient buffering

## Building

```bash
cargo build --release
```

## Running

```bash
./run.sh
# or
./target/release/binance-websocket
```

Service starts on `0.0.0.0:8001`

## Configuration

Edit constants in `src/config.rs`:
- Database path
- Buffer sizes
- Server host/port
- WebSocket URL

## Dependencies

- `tokio` - Async runtime
- `axum` - Web framework
- `tokio-tungstenite` - WebSocket client
- `rusqlite` - SQLite with bundled lib
- `serde/serde_json` - Serialization
- `tracing` - Logging

## Database Schema

- `binance_trades` - Raw trade data
- `binance_candles_{interval}` - Aggregated OHLCV data
- Indexes on time fields for fast queries
