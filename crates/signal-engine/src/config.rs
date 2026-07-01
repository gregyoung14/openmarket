//! Configuration for Signal Engine — v14
//!
//! Reverted to adaptive confirmation and LMSR Softmax weights.
//! See Quant Paper V14 specs for parameter derivation.

// ── Upstream WebSocket services ──
pub const BINANCE_WS_URL: &str = "ws://127.0.0.1:8001/ws";
pub const POLYMARKET_WS_URL: &str = "ws://127.0.0.1:8002/ws";

// ── This service ──
pub const SERVER_HOST: &str = "0.0.0.0";
pub const DEFAULT_SERVER_PORT: u16 = 8003;
pub fn server_port() -> u16 {
    std::env::var("SIGNAL_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_SERVER_PORT)
}
pub const BROADCAST_CHANNEL_SIZE: usize = 2000;

// ── Reconnection ──
pub const RECONNECT_BASE_DELAY_MS: u64 = 500;
pub const MAX_RECONNECT_DELAY_SECS: u64 = 30;

// ── Health freshness thresholds ──
pub const HEALTH_BINANCE_STALE_SECS: u64 = 30;
pub const HEALTH_POLYMARKET_STALE_SECS: u64 = 30;

// ── V14 LMSR Softmax Weights ──
pub const W_DRIFT: f64 = 1.0910;
pub const W_OFI_ACCEL: f64 = 1.4691;
pub const W_SCOREBOARD: f64 = 4.0578;
pub const WHIPSAW_WEIGHT: f64 = -1.4707;

// ── Confidence Calibration ──
/// Weighted component contribution caps before combining into the final logit.
/// These preserve directional ranking while preventing a single component from
/// forcing the model to saturate near 0/1 too early in the market.
pub const DRIFT_CONTRIB_CAP: f64 = 1.50;
pub const OFI_CONTRIB_CAP: f64 = 1.10;
pub const SCOREBOARD_CONTRIB_CAP: f64 = 1.25;
pub const WHIPSAW_CONTRIB_CAP: f64 = 0.50;
/// Temperature scaling applied after component combination.
pub const CONFIDENCE_TEMPERATURE: f64 = 1.75;

// ── Signal Thresholds ──
/// Minimum confidence for confirmation counting and entry gating
pub const ENTRY_CONFIDENCE: f64 = 0.60;

// ── V14 Entry Filters ──
/// Hard price cap
pub const MAX_ENTRY_PRICE: f64 = 0.55;
/// Minimum entry price floor
pub const MIN_ENTRY_PRICE: f64 = 0.15;
/// Minimum EV edge
pub const MIN_EDGE: f64 = 0.08;

// ── Env-var overrides for paper-trade tournament ──
// These read env vars once at startup, falling back to compile-time defaults.
// Used by the multi-strategy runner to test different parameter combinations.
fn env_f64(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
fn env_bool(key: &str, default: bool) -> bool {
    std::env::var(key)
        .ok()
        .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(default)
}
fn env_u64(key: &str, default: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

pub fn entry_confidence() -> f64 {
    env_f64("MIN_CONFIDENCE", ENTRY_CONFIDENCE)
}
pub fn min_edge() -> f64 {
    env_f64("MIN_EDGE_OVERRIDE", MIN_EDGE)
}
pub fn enable_volume_gate() -> bool {
    env_bool("ENABLE_VOLUME_GATE", ENABLE_VOLUME_GATE)
}
pub fn max_entry_price() -> f64 {
    env_f64("MAX_ENTRY_PRICE_OVERRIDE", MAX_ENTRY_PRICE)
}
pub fn min_entry_price() -> f64 {
    env_f64("MIN_ENTRY_PRICE_OVERRIDE", MIN_ENTRY_PRICE)
}
pub fn min_secs_into_market() -> u64 {
    std::env::var("MIN_SECS_OVERRIDE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(MIN_SECS_INTO_MARKET)
}
pub fn max_secs_into_market() -> u64 {
    std::env::var("MAX_SECS_OVERRIDE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(MAX_SECS_INTO_MARKET)
}
pub fn confidence_temperature() -> f64 {
    env_f64("CONFIDENCE_TEMPERATURE", CONFIDENCE_TEMPERATURE).max(1.0)
}
pub fn calibrated_scorer_mode() -> String {
    std::env::var("CALIBRATED_SCORER_MODE")
        .unwrap_or_else(|_| "disabled".to_string())
        .to_ascii_lowercase()
}
pub fn calibrated_model_path() -> Option<String> {
    std::env::var("CALIBRATED_MODEL_PATH")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}
pub fn calibrated_min_ev() -> f64 {
    env_f64("CALIBRATED_MIN_EV", 0.0)
}
pub fn calibrated_score_interval_secs() -> u64 {
    env_u64("CALIBRATED_SCORE_INTERVAL_SECS", 5).max(1)
}

// ── v8: NO Blacklist ──
/// Empty blacklist to allow trading in all hours
pub const BLACKLIST_HOURS_ET: [u32; 0] = [];
/// No day-specific blacklisted slots
pub const BLACKLIST_DOW_HOUR_ET: [(u32, u32); 0] = [];
/// Market-side slippage assumption for edge calculation
pub const SLIPPAGE: f64 = 0.005;

// ── Market Timing ──
/// Don't scan for signals before this many seconds into the market
pub const MIN_SECS_INTO_MARKET: u64 = 60;
/// Stop scanning after this many seconds
pub const MAX_SECS_INTO_MARKET: u64 = 600;
/// Total market duration
pub const MARKET_DURATION_SECS: u64 = 900;
/// Minimum trades needed before computing drift
pub const MIN_TRADES_FOR_SIGNAL: usize = 20;

// ── Sigmoid Scaling & Distributions ──
pub const STUDENT_T_DF: f64 = 3.0;
pub const SCOREBOARD_SCALE: f64 = 500.0;
pub const OFI_SCALE: f64 = 5.0;
pub const WHIPSAW_OPTIMAL: f64 = 0.40;
pub const WHIPSAW_WIDTH: f64 = 0.08;

// ── Regime Detection (kept but won't block entries in v8 mode) ──
/// Path efficiency ≥ this AND autocorr > -0.10 → trend regime
pub const REGIME_TREND_THRESHOLD: f64 = 0.15;
/// Path efficiency < this → chop regime
pub const REGIME_CHOP_THRESHOLD: f64 = 0.06;
/// Autocorrelation < this → hard override to chop
pub const REGIME_AUTOCORR_CHOP: f64 = -0.25;
/// Number of seconds of 1s close prices for regime lookback
pub const REGIME_LOOKBACK: usize = 60;
/// Confidence penalty applied when regime = neutral
pub const NEUTRAL_CONF_PENALTY: f64 = 0.02;

// ── Adaptive Confirmation Window ──
/// Base confirmation window
pub const BASE_CONFIRM_WINDOW: u64 = 30;
/// Minimum confirmation window
pub const MIN_CONFIRM_WINDOW: u64 = 15;
/// Maximum confirmation window
pub const MAX_CONFIRM_WINDOW: u64 = 50;
/// Typical 1-second log-return std for BTC (normalization constant)
pub const TYPICAL_VOL: f64 = 0.0002;

// ── 1-Second Bar Aggregation ──
/// Minimum 1-second bars before computing signal
pub const MIN_1S_BARS_FOR_SIGNAL: usize = 15;

// ── Volume Gate ──
pub const ENABLE_VOLUME_GATE: bool = true;
pub const VOLUME_MEDIAN_OBSERVATIONS: usize = 168;
pub const MIN_VOLUME_OBSERVATIONS: usize = 24;

// ── Signal Scan Interval ──
/// How often to recompute the signal (ms) during active market scanning
pub const SIGNAL_SCAN_INTERVAL_MS: u64 = 1000;

/// Compute the US Eastern Time UTC offset in seconds for a given UTC epoch.
/// Handles EST (UTC-5) / EDT (UTC-4) transitions using the US rule:
///   - Spring forward: 2nd Sunday of March at 02:00 local → 07:00 UTC
///   - Fall back: 1st Sunday of November at 02:00 local → 06:00 UTC
fn et_offset_secs(epoch_s: i64) -> i64 {
    // Compute year from epoch using a rough method, then refine
    let days = epoch_s.div_euclid(86400);
    // Approximate year (365.25 days per year from epoch 1970)
    let mut year = 1970 + (days as f64 / 365.25) as i64;
    // Refine: make sure we have the right year
    let year_start = year_to_epoch(year);
    if epoch_s < year_start {
        year -= 1;
    }

    // 2nd Sunday of March at 07:00 UTC (02:00 EST)
    let mar1 = if is_leap(year) {
        year_to_epoch(year) + 60 * 86400
    } else {
        year_to_epoch(year) + 59 * 86400
    };
    let mar1_dow = (mar1.div_euclid(86400) + 4).rem_euclid(7); // 0=Sun
    let first_sun_mar = if mar1_dow == 0 { 0 } else { 7 - mar1_dow };
    let second_sun_mar = first_sun_mar + 7;
    let spring_forward = mar1 + second_sun_mar * 86400 + 7 * 3600; // 07:00 UTC

    // 1st Sunday of November at 06:00 UTC (02:00 EDT)
    let days_to_nov1: i64 = if is_leap(year) { 305 } else { 304 };
    let nov1 = year_to_epoch(year) + days_to_nov1 * 86400;
    let nov1_dow = (nov1.div_euclid(86400) + 4).rem_euclid(7); // 0=Sun
    let first_sun_nov = if nov1_dow == 0 { 0 } else { 7 - nov1_dow };
    let fall_back = nov1 + first_sun_nov * 86400 + 6 * 3600; // 06:00 UTC

    if epoch_s >= spring_forward && epoch_s < fall_back {
        -4 * 3600 // EDT
    } else {
        -5 * 3600 // EST
    }
}

fn year_to_epoch(year: i64) -> i64 {
    let y = year - 1970;
    let leap_days = (year - 1969) / 4 - (year - 1901) / 100 + (year - 1601) / 400;
    (y * 365 + leap_days) * 86400
}

fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// Convert UTC epoch seconds to ET day-of-week and ET hour.
/// Returns (dow, hour) where dow: 0=Monday..6=Sunday.
/// Automatically handles EST/EDT transitions.
pub fn et_day_hour(epoch_s: i64) -> (u32, u32) {
    let offset = et_offset_secs(epoch_s);
    let et_epoch = epoch_s + offset; // offset is negative (-5h or -4h)
    let et_hour = et_epoch.rem_euclid(86400) / 3600;
    let days_since_epoch = et_epoch.div_euclid(86400);
    let dow = (days_since_epoch + 3).rem_euclid(7) as u32; // epoch Thu=3 → 0=Mon
    (dow, et_hour as u32)
}

/// Check whether a market opening timestamp should be blacklisted.
pub fn is_blacklisted_epoch(epoch_s: i64) -> bool {
    let (dow, hour) = et_day_hour(epoch_s);
    BLACKLIST_HOURS_ET.contains(&hour)
        || BLACKLIST_DOW_HOUR_ET
            .iter()
            .any(|(slot_dow, slot_hour)| *slot_dow == dow && *slot_hour == hour)
}
