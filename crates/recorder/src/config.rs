pub const SERVER_HOST: &str = "0.0.0.0";
pub const SERVER_PORT: u16 = 8005;

pub fn server_port() -> u16 {
    std::env::var("RECORDER_PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(SERVER_PORT)
}

pub const DEFAULT_DATABASE_FILE: &str = "data/openmarket.db";
pub const DEFAULT_EXPORT_DIR: &str = "data/ml_exports";

pub fn database_file() -> String {
    std::env::var("DATABASE_FILE").unwrap_or_else(|_| DEFAULT_DATABASE_FILE.to_string())
}

pub fn export_dir() -> String {
    std::env::var("ML_EXPORT_DIR").unwrap_or_else(|_| DEFAULT_EXPORT_DIR.to_string())
}

pub const BINANCE_WS_URL: &str = "ws://127.0.0.1:8001/ws";
pub const POLYMARKET_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

pub const DB_CHANNEL_SIZE: usize = 20_000;
pub const DB_BATCH_SIZE: usize = 500;
pub const DB_FLUSH_MS: u64 = 500;

pub const LAG_WINDOW_MS: i64 = 750;
pub const LAG_LOOP_MS: u64 = 500;
pub const LAG_FETCH_BATCH: usize = 1000;

pub const PING_INTERVAL_SECS: u64 = 30;
pub const PONG_TIMEOUT_SECS: u64 = 120;
