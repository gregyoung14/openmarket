# Dataset Card: OpenMarket BTC Polymarket

## Dataset Summary

OpenMarket BTC Polymarket is a high-frequency dataset pairing Binance BTC/USDT
trade data with Polymarket BTC binary market order book events. It is intended
for prediction-market microstructure research, feature engineering, supervised
learning, and reproducible backtesting.

## Data Sources

- Binance BTC/USDT trade WebSocket stream
- Polymarket CLOB WebSocket order book, trade, and last-trade-price events
- Polymarket market metadata from Gamma/CLOB APIs

## Time Precision

Timestamps are stored in milliseconds since Unix epoch. Tables preserve both
source timestamps and ingest timestamps where available.

## Suggested Splits

- Train: earlier contiguous date ranges
- Validation: later non-overlapping ranges
- Test: most recent non-overlapping ranges

Walk-forward validation is preferred over random row splits.

## Known Limitations

- Exchange outages and WebSocket reconnects may introduce gaps.
- Ingest timestamps reflect collector host clocks.
- Backtests using top-of-book prices may overestimate fill quality.
- Market behavior changes over time; historical performance may not transfer.
