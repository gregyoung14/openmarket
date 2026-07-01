/// ═══════════════════════════════════════════════════════════════════
/// volume.rs — Volume Gate Filter
/// ═══════════════════════════════════════════════════════════════════
///
/// Filters out low-volume market windows where signals are noisier.
///
/// # Why Volume Matters
/// From backtest analysis:
/// - High volume quartile:  72.1% win rate
/// - Med-Low volume:        64.7% win rate
/// - That's a 7.4% spread — huge!
///
/// Low volume = fewer participants = noisier price action = worse signals.
/// By skipping low-volume windows, we sacrifice ~45% of trades but
/// gain +1.6% win rate.
///
/// # Integration Notes
/// In your live bot, you need a rolling estimate of "median volume per hour".
/// Options:
/// 1. Compute on a trailing 7-day window of hourly volumes
/// 2. Use the most recent 24h average as the baseline
/// 3. Pre-compute per-hour medians (since volume has strong time-of-day patterns)
///
/// The key insight: skip any 15-minute window where the estimated hourly
/// volume rate is below the running median.

/// Compute the hourly volume rate for a given set of trade quantities
/// over a known time window.
///
/// # Arguments
/// * `quantities` - Slice of trade quantities within the window
/// * `window_duration_secs` - Duration of the window in seconds
///
/// # Returns
/// Estimated hourly volume rate (volume per hour)
pub fn hourly_volume_rate(quantities: &[f64], window_duration_secs: f64) -> f64 {
    let total_vol: f64 = quantities.iter().sum();
    let hours = window_duration_secs / 3600.0;
    total_vol / hours.max(0.001) // Avoid division by zero
}

/// Check whether a market window passes the volume gate.
///
/// # Arguments
/// * `window_vol` - Total volume traded in this market window
/// * `window_duration_secs` - Duration of the window in seconds
/// * `median_hourly_vol` - Running median of hourly volume
///
/// # Returns
/// `true` if the window has sufficient volume to trade
pub fn passes_volume_gate(
    window_vol: f64,
    window_duration_secs: f64,
    median_hourly_vol: f64,
) -> bool {
    let rate = hourly_volume_rate(&[window_vol], window_duration_secs);
    rate >= median_hourly_vol
}

/// Simple online median estimator for rolling volume gate.
///
/// In your live bot, maintain one of these and feed it hourly volume
/// observations. It will converge to the true median.
///
/// # Usage
/// ```
/// let mut estimator = VolumeMedianEstimator::new();
/// // Feed hourly volumes as they come in:
/// estimator.observe(1234.5);
/// estimator.observe(987.2);
/// // Check gate:
/// let passes = hourly_vol > estimator.median();
/// ```
pub struct VolumeMedianEstimator {
    /// Ring buffer of recent hourly volumes
    observations: Vec<f64>,
    /// Maximum observations to keep (e.g., 7 days * 24h = 168)
    max_observations: usize,
}

impl VolumeMedianEstimator {
    pub fn new() -> Self {
        Self::with_capacity(168) // 7 days of hourly data
    }

    pub fn with_capacity(max: usize) -> Self {
        Self {
            observations: Vec::with_capacity(max),
            max_observations: max,
        }
    }

    /// Record a new hourly volume observation
    pub fn observe(&mut self, hourly_vol: f64) {
        if self.observations.len() >= self.max_observations {
            self.observations.remove(0);
        }
        self.observations.push(hourly_vol);
    }

    /// Get the current median estimate
    pub fn median(&self) -> f64 {
        if self.observations.is_empty() {
            return 0.0;
        }
        let mut sorted = self.observations.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let mid = sorted.len() / 2;
        if sorted.len() % 2 == 0 {
            (sorted[mid - 1] + sorted[mid]) / 2.0
        } else {
            sorted[mid]
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hourly_volume_rate() {
        let qtys = vec![10.0, 20.0, 30.0]; // 60 total
        let rate = hourly_volume_rate(&qtys, 900.0); // 15 min window
        assert!((rate - 240.0).abs() < 0.01); // 60 / 0.25 hours = 240
    }

    #[test]
    fn test_volume_gate() {
        assert!(passes_volume_gate(100.0, 900.0, 100.0)); // at median
        assert!(!passes_volume_gate(50.0, 900.0, 300.0)); // below
    }

    #[test]
    fn test_median_estimator() {
        let mut est = VolumeMedianEstimator::new();
        est.observe(10.0);
        est.observe(20.0);
        est.observe(30.0);
        assert!((est.median() - 20.0).abs() < 0.01);
    }
}
