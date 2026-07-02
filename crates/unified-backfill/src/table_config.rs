pub struct TableConfig {
    pub name: &'static str,
    pub dedupe_cols: &'static [&'static str],
    pub order_cols: &'static [&'static str],
    pub unpartitioned: bool,
}

pub const TABLES: &[TableConfig] = &[
    TableConfig {
        name: "binance_trades",
        dedupe_cols: &["trade_id"],
        order_cols: &["received_at", "trade_time"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_ticks_ms",
        dedupe_cols: &["source_ts_ms", "trade_time_ms", "price", "volume"],
        order_cols: &["ingest_ts_ms", "id"],
        unpartitioned: false,
    },
    TableConfig {
        name: "polymarket_ticks_ms",
        dedupe_cols: &[
            "source_ts_ms",
            "market_slug",
            "asset_id",
            "side_label",
            "event_type",
            "price",
            "best_bid",
            "best_ask",
            "size",
        ],
        order_cols: &["ingest_ts_ms", "id"],
        unpartitioned: false,
    },
    TableConfig {
        name: "lag_pairs_ms",
        dedupe_cols: &[
            "paired_at_ms",
            "market_slug",
            "side_label",
            "binance_source_ts_ms",
            "polymarket_source_ts_ms",
            "polymarket_bid",
        ],
        order_cols: &["id"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_candles_1s",
        dedupe_cols: &["candle_start"],
        order_cols: &["created_at"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_candles_5s",
        dedupe_cols: &["candle_start"],
        order_cols: &["created_at"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_candles_1m",
        dedupe_cols: &["candle_start"],
        order_cols: &["created_at"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_candles_5m",
        dedupe_cols: &["candle_start"],
        order_cols: &["created_at"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_candles_15m",
        dedupe_cols: &["candle_start"],
        order_cols: &["created_at"],
        unpartitioned: false,
    },
    TableConfig {
        name: "binance_candles_1h",
        dedupe_cols: &["candle_start"],
        order_cols: &["created_at"],
        unpartitioned: false,
    },
];

pub fn table_by_name(name: &str) -> Option<&'static TableConfig> {
    TABLES.iter().find(|t| t.name == name)
}