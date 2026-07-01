/// Configuration for Signal Engine — v8 Rollback (more volume for bug-fix validation)
///
/// Rolled back from v11 to v8 parameters to maximize trade volume while
/// validating the side-inversion bugfix. Key changes:
///   - v8 signal weights: drift (45%), scoreboard (25%), OFI (20%), EMA (10%)
///   - Lower confidence threshold (0.55), higher price cap (0.75)
///   - No blacklist, no volume gate
///   - Fixed 45s confirmation (via min=max=45)

// ── Upstream WebSocket services ──
pub const BINANCE_WS_URL: &str = "ws://127.0.0.1:8001/ws";
pub const POLYMARKET_WS_URL: &str = "ws://127.0.0.1:8002/ws";

// ── This service ──
pub const SERVER_HOST: &str = "0.0.0.0";
pub const SERVER_PORT: u16 = 8003;
pub const BROADCAST_CHANNEL_SIZE: usize = 2000;

// ── Reconnection ──
pub const RECONNECT_BASE_DELAY_MS: u64 = 500;
pub const MAX_RECONNECT_DELAY_SECS: u64 = 30;

// ── v8 Signal Weights (4-component: drift, scoreboard, OFI, EMA residual) ──
pub const DRIFT_WEIGHT: f64 = 0.45;
pub const SCOREBOARD_WEIGHT: f64 = 0.25;
pub const OFI_ACCEL_WEIGHT: f64 = 0.20;
// Remaining 0.10 goes to whipsaw/EMA residual automatically

// ── Signal Thresholds ──
/// Minimum confidence for confirmation counting and entry gating (v8: 0.55)
pub const ENTRY_CONFIDENCE: f64 = 0.55;

// ── v8 Entry Filters (permissive) ──
/// Hard price cap — v8 used 0.75 (much looser than v11's 0.55)
pub const MAX_ENTRY_PRICE: f64 = 0.75;
/// Minimum entry price floor — keep this to avoid penny bets
pub const MIN_ENTRY_PRICE: f64 = 0.15;
/// Minimum EV edge (v8: 0.05, less strict than v11's 0.08)
pub const MIN_EDGE: f64 = 0.05;

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

// ── Sigmoid Scaling ──
pub const SCOREBOARD_SCALE: f64 = 1000.0;
pub const OFI_SCALE: f64 = 3.0;
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

// ── Fixed 45s Confirmation Window (v8 style) ──
/// Base confirmation window (seconds)
pub const BASE_CONFIRM_WINDOW: u64 = 45;
/// Min and max set to 45 to effectively fix the window
pub const MIN_CONFIRM_WINDOW: u64 = 45;
/// Maximum confirmation window
pub const MAX_CONFIRM_WINDOW: u64 = 45;
/// Typical 1-second log-return std for BTC (normalization constant)
pub const TYPICAL_VOL: f64 = 0.0002;

// ── 1-Second Bar Aggregation ──
/// Minimum 1-second bars before computing signal
pub const MIN_1S_BARS_FOR_SIGNAL: usize = 15;

// ── Volume Gate — DISABLED for v8 ──
pub const ENABLE_VOLUME_GATE: bool = false;
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
