use serde::Serialize;

// ================================================================
// Inbound trade from Binance upstream
// ================================================================

/// A single Binance trade stored in the trade buffer
#[derive(Debug, Clone)]
pub struct BinanceTrade {
    pub trade_time_ms: i64,
    pub price: f64,
    pub quantity: f64,
    pub is_buyer_maker: bool,
}

// ================================================================
// 1-second bar aggregation (v9)
// ================================================================

/// Aggregated 1-second bars built from the raw trade buffer
#[derive(Debug, Clone)]
pub struct OneSecondBars {
    /// Close price per second (forward-filled)
    pub close: Vec<f64>,
    /// Taker buy volume per second
    pub buy_vol: Vec<f64>,
    /// Taker sell volume per second
    pub sell_vol: Vec<f64>,
}

// ================================================================
// Regime classification (v9)
// ================================================================

/// Market regime detected from path efficiency + autocorrelation
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Regime {
    /// Path efficiency ≥ 0.15 and autocorr > -0.10 — full confidence
    Trend,
    /// Between trend and chop — -0.02 confidence penalty
    Neutral,
    /// Path efficiency < 0.06 or autocorr < -0.25 — skip entirely
    Chop,
}

impl std::fmt::Display for Regime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Regime::Trend => write!(f, "trend"),
            Regime::Neutral => write!(f, "neutral"),
            Regime::Chop => write!(f, "chop"),
        }
    }
}

// ================================================================
// Drift signal components
// ================================================================

/// Result of the 3-component v9 regime-aware drift estimator
#[derive(Debug, Clone, Serialize)]
pub struct DriftSignal {
    /// "UP" or "DOWN"
    pub direction: String,
    /// Combined confidence (0.5 → 1.0), after neutral penalty
    pub confidence: f64,
    /// Detected regime (trend/neutral/chop)
    pub regime: Regime,
    /// Path efficiency (0.0 to 1.0) — net displacement / total path
    pub path_eff: f64,
    /// Lag-1 autocorrelation of log returns (-1.0 to 1.0)
    pub autocorr: f64,
    /// Brownian drift P(UP at close)
    pub drift_prob_up: f64,
    /// Drift mu (per second)
    pub drift_mu: f64,
    /// Drift sigma (per second)
    pub drift_sigma: f64,
    /// OFI acceleration: recent - earlier (raw value, -2 to +2)
    pub ofi_accel: f64,
    /// OFI acceleration sigmoid signal
    pub ofi_accel_signal: f64,
    /// Price vs open (fractional)
    pub scoreboard: f64,
    /// Scoreboard sigmoid signal (scale=1000)
    pub scoreboard_signal: f64,
    /// Combined P(UP) before regime penalty
    pub combined_prob_up: f64,
    /// Fraction of 3 components agreeing (0.0 to 1.0)
    pub consistency: f64,
    /// Adaptive confirmation window (seconds)
    pub adaptive_confirm: u64,
    /// Recent 1-second log-return volatility
    pub vol_1s: f64,
}

// ================================================================
// Confirmation state
// ================================================================

/// Tracks the adaptive confirmation window (v9: 15–50s based on volatility)
#[derive(Debug, Clone)]
pub struct ConfirmationState {
    /// Direction being confirmed
    pub direction: Option<String>,
    /// Consecutive seconds in same direction
    pub count: u64,
    /// Wall-clock second when confirmation started
    pub start_secs_in: u64,
}

impl Default for ConfirmationState {
    fn default() -> Self {
        Self {
            direction: None,
            count: 0,
            start_secs_in: 0,
        }
    }
}

impl ConfirmationState {
    /// Feed a new signal tick. Returns true if confirmation window is met.
    pub fn update(&mut self, direction: &str, confidence: f64, min_confidence: f64, secs_in: u64, window: u64) -> bool {
        if confidence < min_confidence {
            // Below threshold — don't count but don't reset either
            return false;
        }

        if self.direction.as_deref() == Some(direction) {
            self.count += 1;
        } else {
            self.direction = Some(direction.to_string());
            self.count = 1;
            self.start_secs_in = secs_in;
        }

        self.count >= window
    }

    pub fn reset(&mut self) {
        self.direction = None;
        self.count = 0;
        self.start_secs_in = 0;
    }
}

// ================================================================
// Market state
// ================================================================

/// Current market info from Polymarket
#[derive(Debug, Clone, Serialize)]
pub struct MarketInfo {
    pub slug: String,
    pub start_ms: i64,
    pub end_ms: i64,
    pub up_price: f64,
    pub down_price: f64,
    pub up_best_ask: f64,
    pub down_best_ask: f64,
    pub up_best_bid: f64,
    pub down_best_bid: f64,
}

// ================================================================
// Outbound signals (broadcast to downstream WS clients)
// ================================================================

/// Signal broadcast to execution engine via /ws
#[derive(Debug, Clone, Serialize)]
pub struct SignalMessage {
    #[serde(rename = "type")]
    pub msg_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub direction: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub consistency: Option<f64>,
    /// Backward compat: execution engine expects raw_prob (= combined_prob_up)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_prob: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub combined_prob_up: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub drift_prob_up: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub market: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secs_in: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secs_left: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub entry_ask: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub entry_bid: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub btc_price: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub n_trades: Option<usize>,
    pub timestamp: i64,
    /// EV edge (confidence - entry_price_with_slippage)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub edge: Option<f64>,
    /// Detected regime (v9)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub regime: Option<String>,
    /// Path efficiency (v9)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path_eff: Option<f64>,
    /// Lag-1 autocorrelation (v9)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub autocorr: Option<f64>,
    /// OFI acceleration raw value (v9)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ofi_accel: Option<f64>,
    /// Adaptive confirmation window in seconds (v9)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub adaptive_confirm: Option<u64>,
    /// Recent 1s volatility (v9)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vol_1s: Option<f64>,
    /// Signal engine version
    pub version: String,
}

// ================================================================
// Engine stats for health endpoint
// ================================================================

#[derive(Debug, Clone, Serialize, Default)]
pub struct EngineStats {
    pub version: String,
    pub binance_trades_buffered: u64,
    pub binance_trades_total: u64,
    pub poly_ticks_received: u64,
    pub signals_computed: u64,
    pub signals_confirmed: u64,
    pub entries_fired: u64,
    pub binance_ws_connected: bool,
    pub poly_ws_connected: bool,
    pub current_market: Option<String>,
    pub market_start_ms: Option<i64>,
    pub last_btc_price: Option<f64>,
    pub last_signal_direction: Option<String>,
    pub last_signal_confidence: Option<f64>,
    pub last_signal_time: Option<i64>,
    pub confirmation_count: u64,
    pub confirmation_direction: Option<String>,
    pub uptime_secs: u64,
    /// Last detected regime (v9)
    pub last_regime: Option<String>,
    /// Last path efficiency (v9)
    pub last_path_eff: Option<f64>,
    /// Last adaptive confirmation window (v9)
    pub last_adaptive_confirm: Option<u64>,
}
