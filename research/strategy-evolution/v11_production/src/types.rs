/// ═══════════════════════════════════════════════════════════════════
/// types.rs — V11 Type Definitions
/// ═══════════════════════════════════════════════════════════════════
///
/// All data structures used by the signal engine and backtester.
/// These map directly to what your live bot should use.

use serde::{Deserialize, Serialize};

// ─────────────────────────────────────────────────────────────────
// Market Regime
// ─────────────────────────────────────────────────────────────────

/// The three market microstructure regimes.
///
/// The strategy only trades in Trend and Neutral.
/// Chop is a "no-trade" zone — the market is mean-reverting and
/// directional signals are unreliable.
///
/// # Win Rates (from backtest)
/// - Trend:   ~68.3% (strategy's sweet spot)
/// - Neutral: ~64.6% (tradeable but with penalty)
/// - Chop:    skipped entirely (resets confirmation count)
#[derive(Debug, Clone, PartialEq)]
pub enum Regime {
    /// Price moving directionally — path_eff ≥ 0.15, autocorr > -0.10.
    /// Best regime for this strategy. Enter with full confidence.
    Trend,

    /// Price going nowhere despite movement — too noisy to trade.
    /// Triggered by: path_eff < 0.06 OR autocorr < -0.25.
    Chop,

    /// Between Trend and Chop — tradeable but apply confidence penalty.
    Neutral,
}

impl std::fmt::Display for Regime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Regime::Trend => write!(f, "Trend"),
            Regime::Chop => write!(f, "Chop"),
            Regime::Neutral => write!(f, "Neutral"),
        }
    }
}

// ─────────────────────────────────────────────────────────────────
// Signal Result
// ─────────────────────────────────────────────────────────────────

/// The output of `compute_signal()` — everything the execution engine
/// needs to decide whether to enter a trade.
///
/// # Integration Notes
/// In your live bot, call `compute_signal()` every second during the
/// entry window. Track the result with the highest `confidence` and
/// enter at that point (best-signal mode).
#[derive(Debug, Clone)]
pub struct SignalResult {
    /// Predicted direction: "UP" or "DOWN".
    pub direction: String,

    /// Model confidence in the prediction (0.5 = no edge, 1.0 = certain).
    /// After slippage and fees, you need ≥ 0.585 for positive expected value.
    pub confidence: f64,

    /// Current market regime — determines whether to trade.
    pub regime: Regime,

    /// Path efficiency from regime detection (0.0–1.0).
    /// Higher = more directional movement. Useful for logging/analysis.
    pub path_eff: f64,

    /// Return autocorrelation from regime detection (-1.0 to 1.0).
    /// Positive = momentum. Negative = mean-reversion.
    pub autocorr: f64,

    /// Adaptive confirmation window — how many consecutive agreeing
    /// seconds are required before this signal is "confirmed".
    /// Scales inversely with volatility.
    pub adaptive_confirm: i64,

    /// Signal consistency — fraction of sub-components that agree
    /// on the direction (0.33, 0.67, or 1.0).
    /// Not used as a gate in V11 production, but logged for analysis.
    pub consistency: f64,
}

// ─────────────────────────────────────────────────────────────────
// Trade Log
// ─────────────────────────────────────────────────────────────────

/// Record of a single completed trade — used for backtesting output
/// and post-hoc analysis.
#[derive(Debug, Serialize, Clone)]
pub struct TradeLog {
    /// Market identifier (e.g., "btcusdc-1766361600")
    pub slug: String,

    /// How many seconds into the market window the entry was made.
    /// Range: MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET (60–600).
    pub entry_secs_in: i64,

    /// Direction traded: "UP" or "DOWN"
    pub side: String,

    /// Entry price paid (best_ask + slippage)
    pub entry_price: f64,

    /// Exit price received (1.0 if correct, 0.0 if wrong)
    pub exit_price: f64,

    /// Net PnL for this trade (after fees)
    pub pnl: f64,

    /// Running bankroll after this trade
    pub bankroll: f64,

    /// Whether the predicted direction matched the actual outcome
    pub correct: bool,

    /// Signal confidence at entry
    pub conf: f64,

    /// Edge at entry (confidence - entry_price)
    pub edge: f64,

    /// Market regime at time of entry
    pub regime: String,

    /// Path efficiency at time of entry
    pub path_eff: f64,

    /// Return autocorrelation at time of entry
    pub autocorr: f64,

    /// Signal consistency (fraction of sub-signals agreeing)
    pub consistency: f64,
}
