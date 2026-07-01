# Synchronization

Synchronization is the central research contribution of OpenMarket.

The recorder stores both source timestamps and ingest timestamps:

- `source_ts_ms`: timestamp emitted by the exchange or event source
- `ingest_ts_ms`: timestamp when the local collector received the event

For matched Binance and Polymarket events:

```text
lead_lag_ms = polymarket_source_ts_ms - binance_source_ts_ms
```

Positive values indicate that the Polymarket event timestamp follows the Binance
event timestamp. Negative values indicate the opposite.

## Pairing Method

The recorder currently pairs events inside a bounded millisecond window and
records:

- Binance tick ID
- Polymarket tick ID
- market slug
- side label
- source timestamps
- lead/lag
- Binance price
- Polymarket bid
- price delta in basis points
- quality flag

## Data Quality Issues

The public dataset and paper should explicitly report:

- reconnect windows
- duplicate messages
- out-of-order events
- missing market metadata
- clock drift between source and collector
- alignment window sensitivity
- stale book snapshots

## Recommended Validation

1. Verify monotonicity by source timestamp within stream.
2. Count duplicate raw message IDs or identical raw JSON payloads.
3. Track gaps between adjacent events.
4. Plot lead/lag histograms by day and market.
5. Rebuild features from raw partitions and compare checksums.
