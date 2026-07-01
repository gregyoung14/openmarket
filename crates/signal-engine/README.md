# Signal Engine

The **Signal Engine** is the core signal generation service. It consumes real-time Binance and Polymarket data streams and produces trading signals using a pure Rust drift estimator.

## Architecture

```
┌──────────────────────┐   WS    ┌─────────────────────────────────┐
│  Binance WS (8001)   │────────►│                                 │
│  (BTC/USDT trades)   │         │      Signal Engine (8003)       │
└──────────────────────┘         │                                 │
                                 │  1. Ingests real-time data      │
┌──────────────────────┐   WS    │  2. Runs drift estimator        │
│  Polymarket WS (8002)│────────►│  3. Broadcasts entry signals    │
│  (CLOB book/trades)  │         │                                 │
└──────────────────────┘         └──────────┬──────────────────────┘
                                            │
                                       WS   │  entry/prediction signals
                                            ▼
                                   ┌──────────────────────┐
                                   │ Execution Engine      │
                                   │ (8004)                │
                                   └──────────────────────┘
```

## Signal Types

### Prediction (every ~1s)
```json
{
  "type": "prediction",
  "direction": "UP",
  "confidence": 0.72,
  "raw_prob": 0.72,
  "timestamp": 1770937323857,
  "market": "btc-updown-15m-1770937200",
  "secs_in": 123,
  "secs_left": 776
}
```

### Entry Signal (when confidence exceeds threshold)
```json
{
  "type": "entry",
  "action": "ENTER",
  "side": "UP",
  "confidence": 0.85,
  "entry_ask": 0.45,
  "market": "btc-updown-15m-1770937200",
  "market_end_ms": 1770938100000
}
```

## Configuration

Key constants in `src/config.rs`:

| Constant | Value | Description |
|---|---|---|
| `SERVER_PORT` | 8003 | HTTP/WS server port |
| `BASE_CONFIRM_WINDOW` | 30s | Base adaptive confirmation length |
| `MAX_ENTRY_PRICE` | 0.55 | Maximum ask price for signal generation |

## Version

Signal version is defined in `btc-common/src/version.rs`:
- `SIGNAL_VERSION` = `v14`
- `SIGNAL_METHOD` = `drift_estimator_v14_quant_paper`

See [Version Management Guide](../common/VERSION_GUIDE.md) for bump instructions.

## Running

Run locally:
```bash
cargo run -p signal-engine
```

## Building

```bash
cargo build -p signal-engine --release
```
