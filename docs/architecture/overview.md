# Architecture

OpenMarket separates collection, synchronization, research, and execution.

```text
          Binance WS
              |
              v
      Tick Stream Collector
              |
              v
      Timestamp Synchronizer <----- Polymarket WS
              |                          |
              |                          v
              |                  Order Book Collector
              v
       Feature Generator
              |
              v
        ML / Signal Engine
              |
              v
          Backtester
              |
              v
          Evaluation
```

## Crates

| Crate | Purpose |
|---|---|
| `common` | Shared constants and cross-service types |
| `exchange-binance` | Binance BTC/USDT trade stream and candle persistence |
| `exchange-polymarket` | Polymarket CLOB event stream and market subscription |
| `recorder` | Multi-market recording, normalization, lag pairing, exports |
| `signal-engine` | Real-time drift/order-flow signal generation |
| `execution-engine` | Optional live/paper execution and position management |
| `paper-executor` | Paper-trading execution harness |
| `backtester` | Historical backtesting and strategy evaluation |
| `data-prep` | Dataset conversion and preparation utilities |
| `dataset-downloader` | Snapshot downloader utilities |

## Public Release Principle

The public repository should be useful without private infrastructure. Runtime
services are included for reproducibility, but default configs are paper-only and
safe. Data and models are fetched from external artifact stores.
