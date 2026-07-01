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

// ─────────────────────────────────────────────────────────────────
// Signal Weights
// ─────────────────────────────────────────────────────────────────
// These three directional weights sum to 1.02 (intentionally over 1.0).
// The -0.02 residual is used as a whipsaw dampener (see signal.rs).
//
// Backtested optimal weights for V11:
//   Drift (57%) is the primary driver — statistical drift extrapolation.
//   OFI Accel (30%) captures order flow momentum shifts.
//   Scoreboard (15%) is "price above open?" momentum.
//   Whipsaw (-2% residual) dampens overconfidence in good price action.

/// Weight for the drift (log-return extrapolation) signal.
/// This is the core signal — projects observed drift forward using
/// a CDF-based probability. Higher = more reliance on trend continuation.
pub const W_DRIFT: f64 = 0.57;

/// Weight for order flow imbalance acceleration.
/// Compares recent buy/sell pressure vs earlier buy/sell pressure.
/// Captures momentum shifts from institutional flow.
pub const W_OFI_ACCEL: f64 = 0.30;

/// Weight for the scoreboard (price vs open) signal.
/// Uses the original V10 weight of 0.15 with the ORIGINAL SCOREBOARD_SCALE=1000.
/// (Tested: reducing this to 0.08 with scale=300 actually hurts - 0.3% WR.)
pub const W_SCOREBOARD: f64 = 0.15;

/// Whipsaw weight is computed as the REMAINING weight after the three
/// directional components: 1.0 - 0.57 - 0.30 - 0.15 = -0.02.
/// This negative residual acts as a mild confidence dampener —
/// high whipsaw quality slightly reduces overconfidence, which
/// counterintuitively improves win rate by ~1.3%.
/// The remaining weight is calculated dynamically in signal.rs.

// ─────────────────────────────────────────────────────────────────
// Signal Scaling
// ─────────────────────────────────────────────────────────────────

/// Sigmoid scale for the OFI acceleration signal.
/// Maps the raw OFI delta (typically -0.5 to +0.5) through a sigmoid.
/// Higher = more aggressive response to OFI changes.
pub const OFI_SCALE: f64 = 3.0;

/// Sigmoid scale for the scoreboard (price-vs-open) signal.
/// Using V10's original 1000 — which saturates aggressively but works
/// best with the -0.02 whipsaw dampener and W_SCOREBOARD=0.15.
/// A 0.1% move → sigmoid(1.0) ≈ 0.73 (strong signal).
/// A 0.5% move → sigmoid(5.0) ≈ 1.00 (fully saturated).
pub const SCOREBOARD_SCALE: f64 = 1000.0;

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

/// Path efficiency below this → Chop regime (pure noise, skip trading).
pub const REGIME_CHOP_THRESHOLD: f64 = 0.06;

/// Autocorrelation below this → Chop regime (strong mean-reversion).
/// -0.25 is aggressively negative — confirms persistent reversal pattern.
pub const REGIME_AUTOCORR_CHOP: f64 = -0.25;

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

/// Base number of consecutive confirming seconds required.
pub const BASE_CONFIRM_WINDOW: i64 = 30;

/// Minimum confirmation window (even in extreme volatility).
pub const MIN_CONFIRM_WINDOW: i64 = 15;

/// Maximum confirmation window (in dead-calm markets).
pub const MAX_CONFIRM_WINDOW: i64 = 50;

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

/// Whether to enable the volume gate filter.
/// When true, markets with below-median volume are skipped.
/// Reduces trades from ~3300 to ~1800 but adds +1.6% win rate.
pub const ENABLE_VOLUME_GATE: bool = true;

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
/// Below 0.15, books are usually stubby and produce unrealistic penny fills.
pub const DEFAULT_MIN_ENTRY_PRICE: f64 = 0.15;

/// Maximum entry price (best_ask) to accept.
/// Beyond 0.55, the payout asymmetry shrinks too much.
pub const DEFAULT_MAX_ENTRY_PRICE: f64 = 0.55;
