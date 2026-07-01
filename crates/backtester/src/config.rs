/// ═══════════════════════════════════════════════════════════════════
/// config.rs — V11 Production Configuration
/// ═══════════════════════════════════════════════════════════════════
///
/// All tunable parameters for the signal engine live here.
/// When integrating into your live bot, copy this file and adjust
/// values as needed. Each constant is documented with:
///   - What it controls
///   - Why this specific value was chosen
///   - What happens if you raise/lower it
use lazy_static::lazy_static;
use std::env;

fn env_f64(key: &str, default: f64) -> f64 {
    env::var(key)
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(default)
}

fn env_i64(key: &str, default: i64) -> i64 {
    env::var(key)
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(default)
}

fn env_bool(key: &str, default: bool) -> bool {
    env::var(key)
        .ok()
        .and_then(|v| match v.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => Some(true),
            "0" | "false" | "no" | "off" => Some(false),
            _ => None,
        })
        .unwrap_or(default)
}

// ─────────────────────────────────────────────────────────────────
// Signal Weights (LMSR Softmax Formulation)
// ─────────────────────────────────────────────────────────────────
// In V14, we map Polymarket's LMSR mathematically directly to Softmax (Logistic Regression).
// Instead of summing weighted probabilities, we sum weighted logits (log-odds),
// then pass them through a standard softmax (sigmoid for 2 outcomes) function.

lazy_static! {
    /// Weight (Beta) for the drift (Student-t log-odds) signal.
    pub static ref W_DRIFT: f64 = env::var("W_DRIFT").unwrap_or_else(|_| "1.0910".to_string()).parse().unwrap();

    /// Weight (Beta) for order flow imbalance acceleration logic.
    pub static ref W_OFI_ACCEL: f64 = env::var("W_OFI_ACCEL").unwrap_or_else(|_| "1.4691".to_string()).parse().unwrap();

    /// Weight (Beta) for the scoreboard momentum.
    pub static ref W_SCOREBOARD: f64 = env::var("W_SCOREBOARD").unwrap_or_else(|_| "4.0578".to_string()).parse().unwrap();

    /// Penalty weight for whipsaw (acting as confidence dampener in logit space).
    pub static ref WHIPSAW_WEIGHT: f64 = env::var("WHIPSAW_WEIGHT").unwrap_or_else(|_| "-1.4707".to_string()).parse().unwrap();

    /// Path efficiency below this => Chop regime.
    pub static ref REGIME_CHOP_THRESHOLD: f64 = env_f64("REGIME_CHOP_THRESHOLD", 0.06);

    /// Autocorrelation below this => Chop regime.
    pub static ref REGIME_AUTOCORR_CHOP: f64 = env_f64("REGIME_AUTOCORR_CHOP", -0.25);

    /// Base number of consecutive confirming seconds required.
    pub static ref BASE_CONFIRM_WINDOW: i64 = env_i64("BASE_CONFIRM_WINDOW", 30);

    /// Minimum confirmation window (even in extreme volatility).
    pub static ref MIN_CONFIRM_WINDOW: i64 = env_i64("MIN_CONFIRM_WINDOW", 15);

    /// Maximum confirmation window (in dead-calm markets).
    pub static ref MAX_CONFIRM_WINDOW: i64 = env_i64("MAX_CONFIRM_WINDOW", 50);

    /// Whether to enable the volume gate filter.
    pub static ref ENABLE_VOLUME_GATE: bool = env_bool("ENABLE_VOLUME_GATE", true);

    /// Theta penalty multiplier in the time-decay edge penalty.
    pub static ref THETA_WEIGHT: f64 = env_f64("THETA_WEIGHT", 2.0);

    /// Vega penalty multiplier in the time-decay edge penalty.
    pub static ref VEGA_WEIGHT: f64 = env_f64("VEGA_WEIGHT", 1.5);
}

// ─────────────────────────────────────────────────────────────────
// Signal Scaling & Distributions
// ─────────────────────────────────────────────────────────────────

/// Student-t Degrees of Freedom modeling fat tails in financial returns.
/// Used in Maximum Likelihood Estimation of drift instead of normal distribution.
pub const STUDENT_T_DF: f64 = 3.0;

/// Sigmoid scale for the OFI acceleration signal.
pub const OFI_SCALE: f64 = 5.0;

/// Sigmoid scale for the scoreboard (price-vs-open) signal.
pub const SCOREBOARD_SCALE: f64 = 500.0;

/// Optimal whipsaw ratio — the "sweet spot" for price action quality.
/// Backtesting showed ~0.40 (40% of bars change direction) is ideal.
/// Below this = too calm / no signal. Above = too noisy.
pub const WHIPSAW_OPTIMAL: f64 = 0.40;

/// Width of the Gaussian curve around WHIPSAW_OPTIMAL.
/// Controls how sharply the whipsaw signal drops off from the optimum.
/// Smaller = more selective. 0.08 gives a gentle bell curve.
pub const WHIPSAW_WIDTH: f64 = 0.08;

// ─────────────────────────────────────────────────────────────────
// Regime Detection
// ─────────────────────────────────────────────────────────────────
// The regime detector classifies the current market microstructure
// as Trend, Chop, or Neutral based on path efficiency and autocorrelation.
//
// Path efficiency = abs(end - start) / sum(abs(moves))
//   1.0 = perfectly straight line (strong trend)
//   0.0 = went nowhere despite lots of movement (chop)
//
// Autocorrelation of returns:
//   Positive = returns tend to follow direction (trend)
//   Negative = returns tend to reverse (mean-revert / chop)

/// Path efficiency above this → Trend regime (if autocorr also confirms).
/// The strategy performs best in Trend: 68.3% vs 64.6% in Neutral.
pub const REGIME_TREND_THRESHOLD: f64 = 0.15;

/// Number of seconds of price data to use for regime detection.
/// 60 seconds gives enough data for stable path_eff and autocorr estimates.
pub const REGIME_LOOKBACK: usize = 60;

/// Confidence penalty applied when regime is Neutral (not Trend, not Chop).
/// Neutral means we're unsure about market structure — slightly reduce confidence.
pub const NEUTRAL_CONF_PENALTY: f64 = 0.02;

// ─────────────────────────────────────────────────────────────────
// Entry Timing
// ─────────────────────────────────────────────────────────────────
// In each 15-minute market window, the strategy scans seconds 60–600
// looking for a qualifying signal. The "best signal" enhancement
// tracks all qualifying signals and enters at the one with maximum
// confidence rather than taking the first one found.

/// Earliest second into the market window to start scanning for signals.
/// Before 60s, there isn't enough price data for stable regime/signal computation.
pub const MIN_SECS_INTO_MARKET: i64 = 60;

/// Latest second to consider for entry.
/// After 600s (10 min into a 15-min window), only 5 min remain —
/// not enough time for the directional move to play out.
pub const MAX_SECS_INTO_MARKET: i64 = 600;

/// Total duration of each market window in seconds.
/// This defines the binary outcome window: does BTC go UP or DOWN
/// between window start and window end?
pub const MARKET_DURATION_SECS: i64 = 900; // 15 minutes

// ─────────────────────────────────────────────────────────────────
// Confirmation Window
// ─────────────────────────────────────────────────────────────────
// The strategy requires N consecutive seconds of agreeing signals
// before considering an entry. This "adaptive confirm" window scales
// with recent volatility — high vol = shorter confirm (because the
// signal is more decisive), low vol = longer confirm (wait for clarity).

// ─────────────────────────────────────────────────────────────────
// Execution
// ─────────────────────────────────────────────────────────────────

/// Slippage added to best_ask when computing entry price.
/// In Polymarket, you typically pay 0.5¢ above the displayed best ask.
pub const SLIPPAGE: f64 = 0.005;

/// Fee rate per side (entry and exit). Polymarket charges ~1% per trade.
pub const FEE_RATE: f64 = 0.01;

// ─────────────────────────────────────────────────────────────────
// Volume Gate
// ─────────────────────────────────────────────────────────────────
// The volume gate skips markets where hourly trading volume is below
// the dataset median. Low-volume periods have noisier price action
// and produce lower win rates (64.7% vs 72.1% in high volume).
//
// In your live bot, you'd compute this rolling median from recent
// data rather than from the full backtest dataset.

// ─────────────────────────────────────────────────────────────────
// Default CLI values
// ─────────────────────────────────────────────────────────────────

/// Starting bankroll for the backtest simulation.
pub const DEFAULT_BANKROLL: f64 = 100.0;

/// Fraction of bankroll risked per trade (Kelly-lite).
/// 2% is conservative — full Kelly would be higher at 75% WR but
/// real-world variance makes conservative sizing essential.
pub const DEFAULT_BET_FRACTION: f64 = 0.02;

/// Minimum signal confidence required to consider a trade.
/// Below 0.60, the edge is too thin after slippage and fees.
pub const DEFAULT_MIN_CONFIDENCE: f64 = 0.60;

/// Minimum edge (confidence - entry_price) required.
/// With entry at ~0.505, you need confidence ≥ 0.585 (edge ≥ 0.08).
pub const DEFAULT_MIN_EDGE: f64 = 0.08;

/// Minimum entry price (best_ask) to accept.
/// Below 0.15, order books are typically stubby/penny contracts with poor fills.
pub const DEFAULT_MIN_ENTRY_PRICE: f64 = 0.15;

/// Maximum entry price (best_ask) to accept.
/// Beyond 0.55, the payout asymmetry shrinks too much.
pub const DEFAULT_MAX_ENTRY_PRICE: f64 = 0.55;

// ─────────────────────────────────────────────────────────────────
// Option Greeks & Time Decay Parameters
// ─────────────────────────────────────────────────────────────────

pub const MIN_SECS_LEFT_FOR_ENTRY: i64 = 5;

// We use Theta and Vega from explicit Binary Option pricing for time penalty.
// Both are runtime tunable via env vars THETA_WEIGHT and VEGA_WEIGHT.

pub const KELLY_SCALE: f64 = 0.25; // quarter-Kelly
pub const MAX_BET_FRACTION: f64 = 0.05; // hard cap at 5% of bankroll
pub const MIN_BET_SIZE: f64 = 1.0; // minimum bet in units
