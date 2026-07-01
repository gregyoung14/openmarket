//! Drift Estimator — Pure Rust implementation of the v14 production signal.
//!
//! 3-component weighted signal with regime gating:
//!   1. Brownian drift estimator (55%) — projects observed drift to market close
//!   2. OFI acceleration (30%) — split-window detrended order flow momentum
//!   3. Scoreboard (15%) — price vs open direction (reduced sensitivity)
//!
//! Regime detection via path efficiency + lag-1 autocorrelation:
//!   - trend: full confidence, normal entry
//!   - neutral: -0.02 confidence penalty
//!   - chop: skip entirely (reset confirmation)
//!
//! Adaptive confirmation window: 15–50s based on recent volatility.
//!
//! Port of `strategies/v14_x_quant_paper/src/signal.rs::compute_signal()`

use crate::config;
use crate::models::{DriftSignal, OneSecondBars, Regime};

fn capped_contribution(logit: f64, weight: f64, cap: f64) -> f64 {
    (weight * logit).clamp(-cap, cap)
}

fn compute_whipsaw(closes: &[f64]) -> f64 {
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

fn whipsaw_to_signal(whipsaw: f64) -> f64 {
    let deviation = whipsaw - config::WHIPSAW_OPTIMAL;
    (-deviation.powi(2) / config::WHIPSAW_WIDTH).exp()
}

/// Pearson correlation coefficient between two same-length slices
fn pearson_corr(x: &[f64], y: &[f64]) -> f64 {
    let n = x.len() as f64;
    let mean_x = x.iter().sum::<f64>() / n;
    let mean_y = y.iter().sum::<f64>() / n;
    let mut cov = 0.0;
    let mut var_x = 0.0;
    let mut var_y = 0.0;
    for i in 0..x.len() {
        let dx = x[i] - mean_x;
        let dy = y[i] - mean_y;
        cov += dx * dy;
        var_x += dx * dx;
        var_y += dy * dy;
    }
    let denom = (var_x * var_y).sqrt();
    if denom < 1e-15 {
        0.0
    } else {
        cov / denom
    }
}

/// Detect the market regime from 1-second close prices.
///
/// Uses path efficiency (net displacement / total path) and lag-1 autocorrelation
/// of log returns to classify the current market as trend, neutral, or chop.
///
/// Returns (regime, path_efficiency, autocorrelation)
pub fn detect_regime(close_1s: &[f64]) -> (Regime, f64, f64) {
    let n = close_1s.len();
    let lookback = config::REGIME_LOOKBACK.min(n);
    let recent = &close_1s[n - lookback..];

    // Filter valid (finite, > 0)
    let valid: Vec<f64> = recent
        .iter()
        .copied()
        .filter(|&p| p.is_finite() && p > 0.0)
        .collect();

    if valid.len() < config::MIN_1S_BARS_FOR_SIGNAL {
        return (Regime::Neutral, 0.0, 0.0);
    }

    // Path efficiency: net displacement / total distance
    let direct = (valid.last().unwrap() - valid.first().unwrap()).abs();
    let total_path: f64 = valid.windows(2).map(|w| (w[1] - w[0]).abs()).sum();
    let path_eff = direct / (total_path + 1e-12);

    // Lag-1 autocorrelation of log returns
    let returns: Vec<f64> = valid
        .windows(2)
        .map(|w| ((w[1] + 1e-9) / (w[0] + 1e-9)).ln())
        .collect();

    let autocorr = if returns.len() > 5 {
        let x = &returns[..returns.len() - 1];
        let y = &returns[1..];
        let r = pearson_corr(x, y);
        if r.is_nan() {
            0.0
        } else {
            r
        }
    } else {
        0.0
    };

    // Classification decision tree
    if autocorr < config::REGIME_AUTOCORR_CHOP {
        return (Regime::Chop, path_eff, autocorr);
    }
    if path_eff >= config::REGIME_TREND_THRESHOLD && autocorr > -0.10 {
        return (Regime::Trend, path_eff, autocorr);
    }
    if path_eff < config::REGIME_CHOP_THRESHOLD {
        return (Regime::Chop, path_eff, autocorr);
    }
    (Regime::Neutral, path_eff, autocorr)
}

/// Compute the v14 drift signal from 1-second bars.
///
/// # Arguments
/// * `bars` — 1-second aggregated bars (close, buy_vol, sell_vol)
/// * `open_price` — BTC price at market open
/// * `remaining_seconds` — Seconds until market close
///
/// # Returns
/// `Some(DriftSignal)` if enough data, `None` if insufficient
pub fn compute_drift_signal_v14(
    bars: &OneSecondBars,
    open_price: f64,
    _remaining_seconds: f64,
) -> Option<DriftSignal> {
    // Filter valid prices (finite, > 0)
    let valid_prices: Vec<f64> = bars
        .close
        .iter()
        .copied()
        .filter(|&p| p.is_finite() && p > 0.0)
        .collect();

    if valid_prices.len() < config::MIN_1S_BARS_FOR_SIGNAL {
        return None;
    }

    let n = bars.close.len();
    let current_price = *valid_prices.last().unwrap();

    // ═══ REGIME DETECTION ═══
    let (regime, path_eff, autocorr) = detect_regime(&bars.close);

    // ═══ Component 1: Bayesian Posterior (Student-t) ═══
    let log_returns: Vec<f64> = valid_prices
        .windows(2)
        .map(|w| (w[1] / (w[0] + 1e-9)).ln())
        .collect();

    if log_returns.len() < 5 {
        return None;
    }

    let mut log_odds: f64 = 0.0;
    let local_vol: f64 = config::TYPICAL_VOL;
    let expected_move: f64 = 0.00004;

    let nu = config::STUDENT_T_DF;
    let t_scale = (nu + 1.0) / 2.0;

    let total_vols: Vec<f64> = (0..n).map(|i| bars.buy_vol[i] + bars.sell_vol[i]).collect();
    let mut expanding_vol_sum = 0.0;

    for (i, &r_t) in log_returns.iter().enumerate() {
        let current_vol = total_vols[i + 1];
        expanding_vol_sum += current_vol;
        let avg_vol = expanding_vol_sum / (i as f64 + 1.0);

        let z_up = (r_t - expected_move) / local_vol;
        let z_down = (r_t + expected_move) / local_vol;

        let ll_up_t = -t_scale * (1.0 + z_up.powi(2) / nu).ln();
        let ll_down_t = -t_scale * (1.0 + z_down.powi(2) / nu).ln();

        let mut step_odds = ll_up_t - ll_down_t;

        if current_vol > avg_vol * 1.5 {
            step_odds *= 2.0;
        } else if current_vol > avg_vol {
            step_odds *= 1.2;
        } else {
            step_odds *= 0.5;
        }

        log_odds += step_odds;
    }

    let drift_logit = log_odds;
    let drift_prob_up = 1.0 / (1.0 + (-drift_logit).exp());

    let mu = log_returns.iter().sum::<f64>() / log_returns.len() as f64;
    let sigma = {
        let var =
            log_returns.iter().map(|&r| (r - mu).powi(2)).sum::<f64>() / log_returns.len() as f64;
        var.sqrt()
    };

    // ═══ Component 2: OFI Acceleration Signal ═══
    let half = (n / 2).max(5);
    let buy_recent: f64 = bars.buy_vol[n.saturating_sub(half)..].iter().sum();
    let sell_recent: f64 = bars.sell_vol[n.saturating_sub(half)..].iter().sum();
    let buy_earlier: f64 = bars.buy_vol[..half.min(n)].iter().sum();
    let sell_earlier: f64 = bars.sell_vol[..half.min(n)].iter().sum();

    let ofi_recent = (buy_recent - sell_recent) / (buy_recent + sell_recent + 1e-9);
    let ofi_earlier = (buy_earlier - sell_earlier) / (buy_earlier + sell_earlier + 1e-9);
    let ofi_accel = ofi_recent - ofi_earlier;

    let ofi_logit = ofi_accel * config::OFI_SCALE;
    let ofi_accel_signal = 1.0 / (1.0 + (-ofi_logit).exp());

    // ═══ Component 3: Scoreboard Signal ═══
    let price_vs_open = (current_price - open_price) / (open_price + 1e-9);
    let scoreboard_logit = price_vs_open * config::SCOREBOARD_SCALE;
    let scoreboard_signal = 1.0 / (1.0 + (-scoreboard_logit).exp());

    // ═══ Component 4: Whipsaw Quality Signal ═══
    let whipsaw_raw = compute_whipsaw(&bars.close);
    let whipsaw_signal = whipsaw_to_signal(whipsaw_raw);

    // ═══ LMSR / Softmax Combination ═══
    let drift_contrib =
        capped_contribution(drift_logit, config::W_DRIFT, config::DRIFT_CONTRIB_CAP);
    let ofi_contrib = capped_contribution(ofi_logit, config::W_OFI_ACCEL, config::OFI_CONTRIB_CAP);
    let scoreboard_contrib = capped_contribution(
        scoreboard_logit,
        config::W_SCOREBOARD,
        config::SCOREBOARD_CONTRIB_CAP,
    );
    let raw_combined_logit = drift_contrib + ofi_contrib + scoreboard_contrib;

    let dampening_direction = raw_combined_logit.signum() * -1.0;
    let whipsaw_contrib = (config::WHIPSAW_WEIGHT.abs() * whipsaw_signal * dampening_direction)
        .clamp(-config::WHIPSAW_CONTRIB_CAP, config::WHIPSAW_CONTRIB_CAP);
    let combined_logit = (raw_combined_logit + whipsaw_contrib) / config::confidence_temperature();

    let combined_prob_up = 1.0 / (1.0 + (-combined_logit).exp());

    let (direction, mut confidence) = if combined_prob_up > 0.5 {
        ("UP".to_string(), combined_prob_up)
    } else {
        ("DOWN".to_string(), 1.0 - combined_prob_up)
    };

    if let Regime::Neutral = regime {
        confidence -= config::NEUTRAL_CONF_PENALTY;
    }

    // ═══ Adaptive Confirmation Window ═══
    let recent_rets = if log_returns.len() > 30 {
        &log_returns[log_returns.len() - 30..]
    } else {
        &log_returns
    };

    let vol_1s = if recent_rets.len() > 3 {
        let m = recent_rets.iter().sum::<f64>() / recent_rets.len() as f64;
        (recent_rets.iter().map(|r| (r - m).powi(2)).sum::<f64>() / recent_rets.len() as f64).sqrt()
    } else {
        0.0
    };

    let vol_score = (vol_1s / config::TYPICAL_VOL).min(2.0);
    let multiplier = (1.3 - 0.3 * vol_score).max(0.5);
    let adaptive_confirm = ((config::BASE_CONFIRM_WINDOW as f64 * multiplier) as u64)
        .clamp(config::MIN_CONFIRM_WINDOW, config::MAX_CONFIRM_WINDOW);

    // ═══ Signal Consistency ═══
    let signals_agree = [
        drift_logit.abs() > 0.05 && (drift_logit > 0.0) == (combined_logit > 0.0),
        ofi_logit.abs() > 0.05 && (ofi_logit > 0.0) == (combined_logit > 0.0),
        scoreboard_logit.abs() > 0.05 && (scoreboard_logit > 0.0) == (combined_logit > 0.0),
    ];
    let consistency = signals_agree.iter().filter(|&&s| s).count() as f64 / 3.0;

    Some(DriftSignal {
        direction,
        confidence,
        regime,
        path_eff,
        autocorr,
        drift_prob_up,
        drift_mu: mu,
        drift_sigma: sigma,
        ofi_accel,
        ofi_accel_signal,
        scoreboard: price_vs_open,
        scoreboard_signal,
        combined_prob_up,
        consistency,
        adaptive_confirm,
        vol_1s,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::OneSecondBars;

    fn make_bars(prices: &[f64]) -> OneSecondBars {
        let n = prices.len();
        // Simulate buy-heavy market
        let buy_vol: Vec<f64> = (0..n).map(|i| 0.01 + (i as f64) * 0.001).collect();
        let sell_vol: Vec<f64> = (0..n).map(|_| 0.005).collect();
        OneSecondBars {
            close: prices.to_vec(),
            buy_vol,
            sell_vol,
        }
    }

    #[test]
    fn drift_signal_uptrend() {
        // Simulate an uptrend: 1s close prices going up
        let prices: Vec<f64> = (0..100).map(|i| 100_000.0 + i as f64 * 1.0).collect();
        let bars = make_bars(&prices);

        let sig = compute_drift_signal_v14(&bars, 100_000.0, 840.0).unwrap();
        assert_eq!(sig.direction, "UP");
        assert!(sig.confidence > 0.5);
        assert!(sig.confidence < 0.95);
        assert!(sig.drift_prob_up > 0.5);
        assert!(sig.scoreboard > 0.0);
    }

    #[test]
    fn drift_signal_downtrend() {
        let prices: Vec<f64> = (0..100).map(|i| 100_000.0 - i as f64 * 1.0).collect();
        // sell-heavy for downtrend
        let n = prices.len();
        let bars = OneSecondBars {
            close: prices,
            buy_vol: vec![0.005; n],
            sell_vol: (0..n).map(|i| 0.01 + (i as f64) * 0.001).collect(),
        };

        let sig = compute_drift_signal_v14(&bars, 100_000.0, 840.0).unwrap();
        assert_eq!(sig.direction, "DOWN");
        assert!(sig.confidence > 0.5);
        assert!(sig.confidence < 0.95);
    }

    #[test]
    fn drift_signal_insufficient_bars() {
        let bars = make_bars(&[100_000.0; 5]);
        let result = compute_drift_signal_v14(&bars, 100_000.0, 840.0);
        assert!(result.is_none());
    }

    #[test]
    fn regime_detection_trend() {
        // Strongly trending: monotonic price increase
        let prices: Vec<f64> = (0..60).map(|i| 100_000.0 + i as f64 * 10.0).collect();
        let (regime, path_eff, _autocorr) = detect_regime(&prices);
        assert_eq!(regime, Regime::Trend);
        assert!(path_eff > 0.9); // nearly perfect efficiency for monotonic
    }

    #[test]
    fn regime_detection_chop() {
        // Choppy: alternating up/down
        let prices: Vec<f64> = (0..60)
            .map(|i| 100_000.0 + if i % 2 == 0 { 5.0 } else { -5.0 })
            .collect();
        let (regime, _path_eff, _autocorr) = detect_regime(&prices);
        assert_eq!(regime, Regime::Chop);
    }

    #[test]
    fn adaptive_confirm_range() {
        // Verify adaptive confirm stays within bounds
        let prices: Vec<f64> = (0..100).map(|i| 100_000.0 + i as f64 * 0.5).collect();
        let bars = make_bars(&prices);
        let sig = compute_drift_signal_v14(&bars, 100_000.0, 840.0).unwrap();
        assert!(sig.adaptive_confirm >= config::MIN_CONFIRM_WINDOW);
        assert!(sig.adaptive_confirm <= config::MAX_CONFIRM_WINDOW);
    }

    #[test]
    fn pearson_corr_perfect() {
        let x = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let y = vec![2.0, 4.0, 6.0, 8.0, 10.0];
        assert!((pearson_corr(&x, &y) - 1.0).abs() < 1e-10);
    }
}
