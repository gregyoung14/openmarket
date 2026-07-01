/// Configuration constants for the Execution Engine
use std::time::Duration;

// ─── Network ──────────────────────────────────────────────
/// Our signal-engine WS (predictions + market info)
pub const SIGNAL_ENGINE_WS: &str = "ws://127.0.0.1:8003/ws";
/// Our polymarket-websocket service (live bid/ask)
pub const POLYMARKET_WS: &str = "ws://127.0.0.1:8002/ws";
/// Polymarket CLOB API
pub const CLOB_BASE_URL: &str = "https://clob.polymarket.com";

// ─── Server ───────────────────────────────────────────────
pub const SERVER_HOST: &str = "0.0.0.0";
pub const SERVER_PORT: u16 = 8004;
pub const EXECUTION_VERSION: &str = "v15";

// ─── Bankroll & Sizing ────────────────────────────────────
/// On-chain USDC.e is the sole source of truth for bankroll.
/// No internal paper bankroll — wallet balance drives all sizing.
///
/// v15: Half-Kelly criterion sizing
///   bet_fraction = KELLY_MULTIPLIER * edge * confidence
///   clamped to [KELLY_MIN_FRACTION, KELLY_MAX_FRACTION]
/// Falls back to KELLY_MIN_FRACTION when edge is missing.
#[allow(dead_code)]
pub const BET_FRACTION: f64 = 0.02; // legacy fallback (unused with Kelly)
pub const KELLY_MULTIPLIER: f64 = 0.5; // half-Kelly
pub const KELLY_MIN_FRACTION: f64 = 0.01; // 1% floor
pub const KELLY_MAX_FRACTION: f64 = 0.05; // 5% ceiling
pub const SLIPPAGE: f64 = 0.005; // $0.005 per share
pub const FEE_RATE: f64 = 0.01; // 1% per leg
pub const ORDER_SIZE_DECIMALS: u32 = 0; // Force whole-number sizes only
pub const ORDER_PRICE_DECIMALS: u32 = 2; // Clamp price precision for quote amount compatibility

/// Truncate (round down) a value to `decimals` decimal places.
pub fn truncate_decimals(value: f64, decimals: u32) -> f64 {
    if !value.is_finite() {
        return value;
    }

    let factor = 10f64.powi(decimals as i32);
    (value * factor).floor() / factor
}

/// Round up a value to `decimals` decimal places.
pub fn ceil_decimals(value: f64, decimals: u32) -> f64 {
    if !value.is_finite() {
        return value;
    }

    let factor = 10f64.powi(decimals as i32);
    (value * factor).ceil() / factor
}

// ─── Signal Thresholds ────────────────────────────────────
// All trade decisions are made by the signal engine (v9.2 filters).
// The execution engine does NOT apply any independent signal thresholds.
// These are kept only for informational logging if needed.

// ─── Timing ───────────────────────────────────────────────
pub const MARKET_DURATION_SECS: i64 = 900; // 15 minutes

// ─── Risk ─────────────────────────────────────────────────
pub const MAX_OPEN_POSITIONS: usize = 1;

// ─── Strategy ─────────────────────────────────────────────
pub const MOMENTUM_TP: f64 = 0.10; // take-profit for momentum strategy
pub const WIN_THRESHOLD: f64 = 0.90; // price > 0.90 at resolve = win
pub const MAX_ENTRY_PRICE: f64 = 0.99; // cap limit price
pub const ENTRY_RETRY_ATTEMPTS: usize = 3;
pub const ENTRY_RETRY_DELAY_MS: u64 = 200;

// ─── Reconnection ─────────────────────────────────────────
pub const RECONNECT_BASE_DELAY: Duration = Duration::from_millis(500);
pub const MAX_RECONNECT_DELAY: Duration = Duration::from_secs(30);

// ─── Broadcast ────────────────────────────────────────────
pub const BROADCAST_CHANNEL_SIZE: usize = 1000;
// ─── Test Mode ────────────────────────────────────────
/// When TEST_MODE=1, override sizing to exactly 1 share per trade.
pub fn is_test_mode() -> bool {
    std::env::var("TEST_MODE")
        .map(|v| v == "1" || v.to_lowercase() == "true")
        .unwrap_or(false)
}
pub const TEST_MODE_SHARES: f64 = 1.0;
// ─── Private Key Env Var ──────────────────────────────────
pub const PRIVATE_KEY_ENV: &str = "POLYMARKET_PRIVATE_KEY";
