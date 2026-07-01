/// ═══════════════════════════════════════════════════════════════════
/// signal.rs — V11 Signal Engine
/// ═══════════════════════════════════════════════════════════════════
///
/// The core signal computation module. This is the brain of the strategy.
///
/// # Architecture
/// The signal engine has 4 components, each producing a probability
/// that BTC price will go UP:
///
/// 1. **Drift** (57% weight): Statistical extrapolation of observed
///    log-return drift, scaled by remaining time using a normal CDF.
///
/// 2. **OFI Acceleration** (30% weight): Compares recent order flow
///    imbalance against earlier OFI to detect momentum shifts.
///
/// 3. **Scoreboard** (8% weight): Simple momentum — is the current
///    price above the window's opening price?
///
/// 4. **Whipsaw Quality** (5% weight): Measures whether the price
///    action quality is in the "sweet spot" for the strategy.
///
/// These are combined via weighted sum, then the direction with
/// confidence > 0.5 is selected as the prediction.
///
/// # Integration Notes
/// In your live bot:
/// ```
/// // Every second during the entry window:
/// let signal = compute_signal(&closes, &buys, &sells, btc_open, remaining_secs);
/// if let Some(sig) = signal {
///     if sig.regime != Regime::Chop && sig.confidence >= MIN_CONFIDENCE {
///         // Track confirmation count and best signal...
///     }
/// }
/// ```
use statrs::distribution::{ContinuousCDF, Normal, StudentsT};

use crate::config::*;
use crate::types::{Regime, SignalResult};

// ─────────────────────────────────────────────────────────────────
// OPTION GREEKS HELPER
// ─────────────────────────────────────────────────────────────────

/// Prices a Binary (Cash-or-Nothing) Call Option using Black-Scholes.
/// Assuming r = 0 for short-term crypto binary options.
fn binary_call_price(s: f64, k: f64, t_sec: f64, sigma_sec: f64) -> f64 {
    if t_sec <= 0.0 {
        return if s > k { 1.0 } else { 0.0 };
    }
    let d2 = ((s / k).ln() - 0.5 * sigma_sec.powi(2) * t_sec) / (sigma_sec * t_sec.sqrt());
    let normal = Normal::new(0.0, 1.0).unwrap();
    normal.cdf(d2)
}

// ═══════════════════════════════════════════════════════════════════
// REGIME DETECTION
// ═══════════════════════════════════════════════════════════════════

/// Detect the current market regime from recent price history.
///
/// Uses two metrics:
/// - **Path efficiency**: How directional is the price movement?
/// - **Autocorrelation**: Do returns tend to continue or reverse?
///
/// # Arguments
/// * `closes` - Array of 1-second close prices (most recent REGIME_LOOKBACK)
///
/// # Returns
/// (Regime, path_efficiency, autocorrelation)
///
/// # Algorithm
/// 1. Take the last REGIME_LOOKBACK (60) closes
/// 2. Compute path efficiency = |end - start| / sum(|moves|)
/// 3. Compute lag-1 autocorrelation of log returns
/// 4. Classify:
///    - autocorr < -0.25 → Chop (strong mean-reversion)
///    - path_eff ≥ 0.15 && autocorr > -0.10 → Trend
///    - path_eff < 0.06 → Chop (no directional movement)
///    - Otherwise → Neutral
pub fn detect_regime(closes: &[f64]) -> (Regime, f64, f64) {
    let n = closes.len();
    if n < 15 {
        return (Regime::Neutral, 0.0, 0.0);
    }

    // Use only the most recent REGIME_LOOKBACK seconds
    let start_idx = if n > REGIME_LOOKBACK {
        n - REGIME_LOOKBACK
    } else {
        0
    };
    let valid: Vec<f64> = closes[start_idx..]
        .iter()
        .cloned()
        .filter(|&x| x > 0.0)
        .collect();

    if valid.len() < 15 {
        return (Regime::Neutral, 0.0, 0.0);
    }

    // ── Path Efficiency ──
    // Ratio of direct displacement to total path traveled.
    // 1.0 = straight line (perfect trend)
    // 0.0 = lots of movement but no net displacement (chop)
    let direct = (valid[valid.len() - 1] - valid[0]).abs();
    let total_path: f64 = valid.windows(2).map(|w| (w[1] - w[0]).abs()).sum();
    let path_eff = direct / (total_path + 1e-12);

    // ── Log Returns ──
    let returns: Vec<f64> = (1..valid.len())
        .map(|i| (valid[i] / (valid[i - 1] + 1e-9)).ln())
        .collect();

    // ── Lag-1 Autocorrelation ──
    // Positive = momentum (returns follow previous direction)
    // Negative = mean-reversion (returns reverse)
    let autocorr = compute_autocorrelation(&returns);

    // ── Classification ──
    if autocorr < *REGIME_AUTOCORR_CHOP {
        (Regime::Chop, path_eff, autocorr)
    } else if path_eff >= REGIME_TREND_THRESHOLD && autocorr > -0.10 {
        (Regime::Trend, path_eff, autocorr)
    } else if path_eff < *REGIME_CHOP_THRESHOLD {
        (Regime::Chop, path_eff, autocorr)
    } else {
        (Regime::Neutral, path_eff, autocorr)
    }
}

/// Compute lag-1 autocorrelation of a return series.
/// Returns 0.0 if insufficient data.
fn compute_autocorrelation(returns: &[f64]) -> f64 {
    if returns.len() <= 5 {
        return 0.0;
    }

    let x = &returns[..returns.len() - 1]; // lagged
    let y = &returns[1..]; // current
    let n = x.len() as f64;

    let mean_x = x.iter().sum::<f64>() / n;
    let mean_y = y.iter().sum::<f64>() / n;

    let mut numerator = 0.0;
    let mut den_x = 0.0;
    let mut den_y = 0.0;

    for i in 0..x.len() {
        let dx = x[i] - mean_x;
        let dy = y[i] - mean_y;
        numerator += dx * dy;
        den_x += dx * dx;
        den_y += dy * dy;
    }

    let denominator = (den_x * den_y).sqrt();
    if denominator > 0.0 {
        numerator / denominator
    } else {
        0.0
    }
}

// ═══════════════════════════════════════════════════════════════════
// WHIPSAW MEASUREMENT
// ═══════════════════════════════════════════════════════════════════

/// Compute the whipsaw ratio: what fraction of consecutive bars
/// change direction.
///
/// - 0.0 = every bar moves the same direction (pure trend)
/// - 1.0 = every bar reverses (extreme chop)
/// - ~0.4 = moderate chop (the sweet spot for this strategy)
///
/// # Why This Matters
/// Moderate whipsaw (0.3–0.5) means there's genuine price discovery
/// happening — enough volatility for signals to form, but not pure
/// noise. The strategy wins 71% at "Med-High" whipsaw vs 64% at "Low".
pub fn compute_whipsaw(closes: &[f64]) -> f64 {
    if closes.len() < 3 {
        return 0.0;
    }

    let diffs: Vec<f64> = closes.windows(2).map(|w| w[1] - w[0]).collect();
    let signs: Vec<f64> = diffs.iter().map(|d| d.signum()).collect();
    let changes = signs
        .windows(2)
        .filter(|w| w[0] != w[1] && w[0] != 0.0 && w[1] != 0.0)
        .count();

    changes as f64 / (signs.len().max(1) - 1).max(1) as f64
}

/// Convert raw whipsaw ratio to a signal value (0.0–1.0).
/// Uses a Gaussian centered at WHIPSAW_OPTIMAL with width WHIPSAW_WIDTH.
/// Peak at 0.40 whipsaw ratio → signal = 1.0
/// At 0.0 or 0.8 → signal ≈ 0.0
fn whipsaw_to_signal(whipsaw: f64) -> f64 {
    let deviation = whipsaw - WHIPSAW_OPTIMAL;
    (-deviation.powi(2) / WHIPSAW_WIDTH).exp()
}

// ═══════════════════════════════════════════════════════════════════
// MAIN SIGNAL COMPUTATION
// ═══════════════════════════════════════════════════════════════════

/// Compute the directional signal from current market data.
///
/// This is the primary function your live bot should call every second
/// during the entry window (seconds 60–600 of each market).
///
/// # Arguments
/// * `closes` - 1-second close prices from window start to current second
/// * `buy_vols` - Per-second taker buy volume (same length as closes)
/// * `sell_vols` - Per-second taker sell volume (same length as closes)
/// * `btc_start` - BTC price at the very start of the market window
/// * `remaining_secs` - Seconds remaining until the window closes
///
/// # Returns
/// `Some(SignalResult)` with direction, confidence, regime, etc.
/// `None` if insufficient data (< 15 seconds of prices).
///
/// # Signal Flow
/// ```text
/// closes ──► detect_regime() ──► Chop? → None (skip)
///    │                              │
///    │                              ▼ Trend/Neutral
///    ├──► drift_signal() ──────────►│
///    ├──► ofi_accel_signal() ──────►│ weighted sum → combined_prob_up
///    ├──► scoreboard_signal() ─────►│
///    └──► whipsaw_signal() ────────►│
///                                   │
///                                   ▼
///                            direction + confidence
///                            + adaptive_confirm window
/// ```
pub fn compute_signal(
    closes: &[f64],
    buy_vols: &[f64],
    sell_vols: &[f64],
    btc_start: f64,
    remaining_secs: i64,
) -> Option<SignalResult> {
    let n = closes.len();
    if n < 15 {
        return None;
    }

    let current_price = closes[n - 1];
    let (regime, path_eff, autocorr) = detect_regime(closes);

    // ── Component 1: Bayesian Posterior (Student-t) ──
    // Log-space sequential updating using log likelihood ratios.
    // log P(UP|D) = log P(UP) + Σ [log P(D_k | UP) - log P(D_k | DOWN)]

    let log_returns: Vec<f64> = (1..n)
        .map(|i| (closes[i] / (closes[i - 1] + 1e-9)).ln())
        .collect();

    if log_returns.len() < 5 {
        return None;
    }

    let mut log_odds: f64 = 0.0; // Prior = 0.5 -> log_odds = 0
    let local_vol: f64 = 0.0002; // Based on TYPICAL_VOL
    let expected_move: f64 = 0.00004; // Assumed directional drift per second

    // Fat tails: Use Student-t distribution instead of Normal
    // log-pdf for Student-t, ignoring constant terms:
    // log P(x) = -(nu + 1)/2 * log(1 + ((x - mu)/sigma)^2 / nu)
    let nu = STUDENT_T_DF;
    let t_scale = (nu + 1.0) / 2.0;

    // Volume-Conditional Probability Array
    // Find average volume for the valid window so far
    let total_vols: Vec<f64> = (0..n).map(|i| buy_vols[i] + sell_vols[i]).collect();
    // Use an expanding window mean for the volume to mimic real-time state:
    let mut expanding_vol_sum = 0.0;

    for (i, &r_t) in log_returns.iter().enumerate() {
        // i corresponds to the interval ending at closes[i+1], so the volume is total_vols[i+1]
        let current_vol = total_vols[i + 1];
        expanding_vol_sum += current_vol;
        let avg_vol = expanding_vol_sum / (i as f64 + 1.0);

        let z_up = (r_t - expected_move) / local_vol;
        let z_down = (r_t + expected_move) / local_vol;

        // Base Likelihoods
        let ll_up_t = -t_scale * (1.0 + z_up.powi(2) / nu).ln();
        let ll_down_t = -t_scale * (1.0 + z_down.powi(2) / nu).ln();

        let mut step_odds = ll_up_t - ll_down_t;

        // Volume-Conditional Multiplier
        // "A stock goes up 60% of days... on days with above-average volume it goes up 75%."
        // We amplify the log-odds update if the volume driving it is above average,
        // and severely dampen the update if it's on thin air.
        if current_vol > avg_vol * 1.5 {
            step_odds *= 2.0; // High conviction
        } else if current_vol > avg_vol {
            step_odds *= 1.2; // Good conviction
        } else {
            step_odds *= 0.5; // "NOISY BS" - thin volume drift
        }

        log_odds += step_odds;
    }

    // Rather than mapping to prob directly, we will use log_odds in LMSR Softmax later.
    let drift_logit = log_odds;

    // ── Component 2: OFI Acceleration Signal ──
    // Compare order flow imbalance (OFI) in the recent half vs earlier half.
    // OFI = (buy_vol - sell_vol) / (buy_vol + sell_vol)
    // Acceleration = recent_OFI - earlier_OFI
    // Positive acceleration → buying pressure increasing → UP signal.
    let half = (n / 2).max(5);

    let buy_recent: f64 = buy_vols[n - half..].iter().sum();
    let sell_recent: f64 = sell_vols[n - half..].iter().sum();
    let buy_earlier: f64 = buy_vols[..half].iter().sum();
    let sell_earlier: f64 = sell_vols[..half].iter().sum();

    let ofi_recent = (buy_recent - sell_recent) / (buy_recent + sell_recent + 1e-9);
    let ofi_earlier = (buy_earlier - sell_earlier) / (buy_earlier + sell_earlier + 1e-9);
    let ofi_accel = ofi_recent - ofi_earlier;

    let ofi_logit = ofi_accel * OFI_SCALE;

    // ── Component 3: Scoreboard Signal ──
    // Simple momentum: is price above or below the opening price?
    let price_vs_open = (current_price - btc_start) / (btc_start + 1e-9);
    let scoreboard_logit = price_vs_open * SCOREBOARD_SCALE;

    // ── Component 4: Whipsaw Quality Signal ──
    // Measures price action quality — moderate chop is the sweet spot.
    let whipsaw_raw = compute_whipsaw(closes);
    // whipsaw_signal ranges 0.0 -> 1.0 (at peak)
    // We treat this peak as a reason to LOWER logit extremity (combat overconfidence)
    let whipsaw_signal = whipsaw_to_signal(whipsaw_raw);

    // ── LMSR / Softmax Combination ──
    // Map linearly in logit space.
    let mut combined_logit =
        *W_DRIFT * drift_logit + *W_OFI_ACCEL * ofi_logit + *W_SCOREBOARD * scoreboard_logit;

    // Whipsaw acts as a regularizer: pulls the combined_logit closer to 0
    let dampening_direction = combined_logit.signum() * -1.0;
    combined_logit += (*WHIPSAW_WEIGHT).abs() * whipsaw_signal * dampening_direction;

    let combined_prob_up = 1.0 / (1.0 + (-combined_logit).exp());

    // ── Direction & Confidence ──
    let (direction, mut confidence) = if combined_prob_up > 0.5 {
        ("UP".to_string(), combined_prob_up)
    } else {
        ("DOWN".to_string(), 1.0 - combined_prob_up)
    };

    // Penalize confidence in Neutral regime (not as reliable as Trend)
    if let Regime::Neutral = regime {
        confidence -= NEUTRAL_CONF_PENALTY;
    }

    // ── Adaptive Confirmation Window ──
    // High volatility → shorter confirm window (clear signal, act fast)
    // Low volatility → longer confirm window (need more evidence)
    let recent_rets = if log_returns.len() > 30 {
        &log_returns[log_returns.len() - 30..]
    } else {
        &log_returns
    };

    let vol = if recent_rets.len() > 3 {
        let m = recent_rets.iter().sum::<f64>() / recent_rets.len() as f64;
        (recent_rets.iter().map(|r| (r - m).powi(2)).sum::<f64>() / recent_rets.len() as f64).sqrt()
    } else {
        0.0
    };

    let vol_score = (vol / 0.0002).min(2.0);
    let adaptive_confirm = (*BASE_CONFIRM_WINDOW as f64 * (1.3 - 0.3 * vol_score).max(0.5)) as i64;
    let adaptive_confirm = adaptive_confirm.clamp(*MIN_CONFIRM_WINDOW, *MAX_CONFIRM_WINDOW);

    // ── Signal Consistency ──
    // What fraction of the sub-signals agree on the predicted direction?
    // 1.0 = all three directional signals agree (strongest)
    // 0.33 = only one agrees (weakest — yet still past threshold)
    let signals_agree = [
        drift_logit.abs() > 0.05 && (drift_logit > 0.0) == (combined_logit > 0.0),
        ofi_logit.abs() > 0.05 && (ofi_logit > 0.0) == (combined_logit > 0.0),
        scoreboard_logit.abs() > 0.05 && (scoreboard_logit > 0.0) == (combined_logit > 0.0),
    ];
    let consistency = signals_agree.iter().filter(|&&s| s).count() as f64 / 3.0;

    Some(SignalResult {
        direction,
        confidence,
        regime,
        path_eff,
        autocorr,
        adaptive_confirm,
        consistency,
    })
}

// ═══════════════════════════════════════════════════════════════════
// BEST SIGNAL SELECTION
// ═══════════════════════════════════════════════════════════════════

/// Candidate trade found during the entry window scan.
/// Used by the best-signal selector to track the peak confidence signal.
#[derive(Debug, Clone)]
pub struct TradeCandidate {
    /// Second into the market when this signal fired
    pub entry_sec: i64,
    /// Direction: "UP" or "DOWN"
    pub direction: String,
    /// Entry price paid (best_ask + slippage)
    pub entry_price: f64,
    /// Confidence at this second
    pub confidence: f64,
    /// Edge (confidence - entry_price)
    pub edge: f64,
    /// Regime at this second
    pub regime: Regime,
    /// Path efficiency at this second
    pub path_eff: f64,
    /// Autocorrelation at this second
    pub autocorr: f64,
    /// Signal consistency at this second
    pub consistency: f64,
}

/// Scan the entry window and find the best qualifying signal.
///
/// This is the KEY V11 improvement: instead of taking the first signal
/// that passes the confirmation + edge gates, we scan the entire window
/// and take the signal with the highest confidence.
///
/// # How It Works
/// 1. For each second in [MIN_SECS, MAX_SECS]:
///    a. Compute the signal
///    b. If regime is Chop → reset confirmation counter
///    c. If confidence ≥ min_confidence → increment confirmation
///    d. If confirmed for enough consecutive seconds → it's a candidate
///    e. Track the candidate with the highest confidence
/// 2. Return the best candidate (or None if no qualifying signal)
///
/// # Arguments
/// * `close_arr` - Full 900-second close price array for the market
/// * `buy_arr` - Full 900-second buy volume array
/// * `sell_arr` - Full 900-second sell volume array
/// * `btc_start` - Opening price of the market window
/// * `min_confidence` - Minimum confidence threshold (typically 0.60)
/// * `min_edge` - Minimum edge threshold (typically 0.08)
/// * `min_entry_price` - Minimum best ask required to avoid stub markets
/// * `max_entry_price` - Maximum best ask allowed before payout asymmetry collapses
/// * `up_entry_asks` - Last known UP best ask by second in the market window
/// * `down_entry_asks` - Last known DOWN best ask by second in the market window
///
/// # Returns
/// `Some(TradeCandidate)` with the best signal found, or `None`
pub fn find_best_signal(
    close_arr: &[f64],
    buy_arr: &[f64],
    sell_arr: &[f64],
    btc_start: f64,
    min_confidence: f64,
    min_edge: f64,
    min_entry_price: f64,
    max_entry_price: f64,
    up_entry_asks: &[f64],
    down_entry_asks: &[f64],
) -> Option<TradeCandidate> {
    let mut best: Option<TradeCandidate> = None;
    let mut best_conf: f64 = 0.0;

    let mut confirm_count: i64 = 0;
    let mut confirm_dir = String::new();

    for s in MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET {
        let idx = s as usize;
        if idx >= close_arr.len() {
            break;
        }

        let res = match compute_signal(
            &close_arr[..=idx],
            &buy_arr[..=idx],
            &sell_arr[..=idx],
            btc_start,
            MARKET_DURATION_SECS - s,
        ) {
            Some(r) => r,
            None => continue,
        };

        // Chop regime → reset confirmation and skip
        if res.regime == Regime::Chop {
            confirm_count = 0;
            continue;
        }

        if res.confidence >= min_confidence {
            // Track consecutive confirmations in the same direction
            if res.direction == confirm_dir {
                confirm_count += 1;
            } else {
                confirm_dir = res.direction.clone();
                confirm_count = 1;
            }

            // Has the signal been confirmed long enough?
            if confirm_count >= res.adaptive_confirm {
                let remaining_secs = MARKET_DURATION_SECS - s;
                if remaining_secs < MIN_SECS_LEFT_FOR_ENTRY {
                    continue;
                }

                let entry_ask = if confirm_dir == "UP" {
                    up_entry_asks.get(idx).copied().unwrap_or(0.0)
                } else {
                    down_entry_asks.get(idx).copied().unwrap_or(0.0)
                };
                if entry_ask < min_entry_price || entry_ask > max_entry_price {
                    continue;
                }
                let entry_price = entry_ask + SLIPPAGE;

                // ── OPTION GREEKS: THETA AND VEGA DECAY ──
                // Estimate recent pure volatility standard deviation per second
                let current_price = close_arr[idx];
                let bsvol = if res.regime != Regime::Neutral {
                    let log_rets: Vec<f64> = close_arr[idx.saturating_sub(60)..=idx]
                        .windows(2)
                        .map(|w| (w[1] / (w[0] + 1e-9)).ln())
                        .collect();
                    let m = log_rets.iter().sum::<f64>() / log_rets.len().max(1) as f64;
                    let v = log_rets.iter().map(|&r| (r - m).powi(2)).sum::<f64>()
                        / log_rets.len().max(1) as f64;
                    v.sqrt().max(0.00001)
                } else {
                    0.0001
                };

                let t = remaining_secs as f64;

                // Theta: Decay experienced over the next 60 seconds of holding
                let price_now = binary_call_price(current_price, btc_start, t, bsvol);
                let price_decayed =
                    binary_call_price(current_price, btc_start, (t - 60.0).max(0.001), bsvol);
                let theta_penalty = (price_now - price_decayed).abs();

                // Vega: Loss in certainty if local volatility spikes +20%
                let price_high_vol = binary_call_price(current_price, btc_start, t, bsvol * 1.20);
                let vega_penalty = (price_high_vol - price_now).abs();

                let time_penalty = theta_penalty * *THETA_WEIGHT + vega_penalty * *VEGA_WEIGHT;

                let edge = res.confidence - (entry_price + time_penalty);
                if edge < min_edge {
                    continue;
                }

                // Is this the best signal we've seen?
                if res.confidence > best_conf {
                    best_conf = res.confidence;
                    best = Some(TradeCandidate {
                        entry_sec: s,
                        direction: confirm_dir.clone(),
                        entry_price,
                        confidence: res.confidence,
                        edge,
                        regime: res.regime,
                        path_eff: res.path_eff,
                        autocorr: res.autocorr,
                        consistency: res.consistency,
                    });
                }
            }
        } else {
            // Confidence dropped below threshold → reset
            confirm_count = 0;
        }
    }

    best
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_regime_detection_with_trend() {
        // Create an uptrending price series
        let closes: Vec<f64> = (0..100).map(|i| 100000.0 + i as f64 * 10.0).collect();
        let (regime, path_eff, _autocorr) = detect_regime(&closes);
        assert!(matches!(regime, Regime::Trend));
        assert!(path_eff > 0.5); // Very directional
    }

    #[test]
    fn test_regime_detection_with_chop() {
        // Create a choppy price series (oscillating)
        let closes: Vec<f64> = (0..100)
            .map(|i| 100000.0 + if i % 2 == 0 { 10.0 } else { -10.0 })
            .collect();
        let (regime, path_eff, _autocorr) = detect_regime(&closes);
        assert!(matches!(regime, Regime::Chop));
        assert!(path_eff < 0.1); // Very non-directional
    }

    #[test]
    fn test_whipsaw_computation() {
        // Pure trend → low whipsaw
        let trend: Vec<f64> = (0..50).map(|i| 100.0 + i as f64).collect();
        assert!(compute_whipsaw(&trend) < 0.1);

        // Pure alternating → high whipsaw
        let chop: Vec<f64> = (0..50)
            .map(|i| 100.0 + if i % 2 == 0 { 1.0 } else { -1.0 })
            .collect();
        assert!(compute_whipsaw(&chop) > 0.8);
    }

    #[test]
    fn test_whipsaw_signal_peaks_at_optimal() {
        let at_optimal = whipsaw_to_signal(WHIPSAW_OPTIMAL);
        let too_low = whipsaw_to_signal(0.1);
        let too_high = whipsaw_to_signal(0.8);

        assert!(at_optimal > 0.99); // Should be ~1.0 at the peak
        assert!(too_low < 0.35); // 0.325 — well below peak
        assert!(too_high < 0.15); // 0.135 — far from peak
    }
}
