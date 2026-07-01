//! Configuration constants for the Binance WebSocket service

pub const BINANCE_WS_URL: &str = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade";
pub const DATABASE_FILE: &str = "data/openmarket.db";
pub const TRADE_BUFFER_SIZE: usize = 100;
pub const BROADCAST_CHANNEL_SIZE: usize = 1000;
pub const DB_WRITE_CHANNEL_SIZE: usize = 10000;
pub const SERVER_HOST: &str = "0.0.0.0";
pub const SERVER_PORT: u16 = 8001;

/// Candle intervals and their durations in milliseconds
pub const INTERVALS: &[(&str, i64)] = &[
    ("1s", 1000),
    ("5s", 5000),
    ("1m", 60000),
    ("5m", 300000),
    ("15m", 900000),
    ("1h", 3600000),
];

/// Valid intervals for API requests
pub const VALID_INTERVALS: &[&str] = &["1s", "5s", "1m", "5m", "15m", "1h"];

/// Connection keep-alive settings
pub const PING_INTERVAL_SECS: u64 = 45; // Send ping every 45 seconds
pub const PONG_TIMEOUT_SECS: u64 = 120; // Consider dead if no activity for 2 minutes
pub const RECONNECT_BASE_DELAY_MS: u64 = 1000; // Base delay for exponential backoff
pub const MAX_RECONNECT_DELAY_SECS: u64 = 60; // Max backoff delay

/// Health freshness thresholds
pub const HEALTH_UPSTREAM_STALE_SECS: u64 = 30;
pub const HEALTH_BROADCAST_STALE_SECS: u64 = 30;
pub const HEALTH_DB_STALE_SECS: u64 = 60;
