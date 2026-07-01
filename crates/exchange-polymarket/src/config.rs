//! Configuration constants for Polymarket WebSocket service
//! MISSION CRITICAL — zero tolerance for downtime, ms-level freshness.

pub const POLYMARKET_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

pub const SERVER_HOST: &str = "0.0.0.0";
pub const SERVER_PORT: u16 = 8002;

/// Large buffer so we never drop a message even under burst
pub const BROADCAST_CHANNEL_SIZE: usize = 10_000;
/// Near-instant reconnect: 50ms base, 2s max — no waiting around
pub const RECONNECT_BASE_DELAY_MS: u64 = 50;
pub const MAX_RECONNECT_DELAY_SECS: u64 = 2;
/// Keep-alive ping every 10s so we detect dead connections fast
pub const PING_INTERVAL_SECS: u64 = 10;
/// If nothing for 15s, nuke the connection and reconnect
pub const PONG_TIMEOUT_SECS: u64 = 15;
/// If no upstream data for this many seconds, force reconnect
pub const STALE_DATA_TIMEOUT_SECS: u64 = 10;
/// Health should fail loud shortly after freshness is lost.
pub const HEALTH_UPSTREAM_STALE_SECS: u64 = 15;
pub const HEALTH_MARKET_DATA_STALE_SECS: u64 = 15;
/// HTTP timeout for Gamma API calls (seconds) — prevents blocking the select loop
pub const HTTP_TIMEOUT_SECS: u64 = 5;
/// WebSocket connect timeout (seconds) — prevents hanging on DNS/TLS
pub const WS_CONNECT_TIMEOUT_SECS: u64 = 10;
/// Max time for boundary market fetch (retry loop) before giving up
pub const BOUNDARY_FETCH_TIMEOUT_SECS: u64 = 8;
/// 15-minute market window in seconds
pub const MARKET_INTERVAL_SECS: i64 = 900;
