/// ═══════════════════════════════════════════════════════════════════
/// calibration.rs — V15 Brier Score Circuit Breaker & Confidence Calibration
/// ═══════════════════════════════════════════════════════════════════
///
/// Two new features over V14:
///
/// 1. **Rolling Brier Score Circuit Breaker**
///    Tracks Brier score over a rolling window of recent trades.
///    When Brier exceeds a threshold (model is miscalibrated), trading
///    is paused until calibration recovers.
///
///    Brier Score = (1/N) * Σ (confidence - outcome)²
///    - 0.00 = perfect calibration
///    - 0.25 = random guessing (50/50 at 50% confidence)
///    - Above 0.25 = worse than random → STOP
///
/// 2. **Confidence-Bin Calibration Table**
///    Tracks actual win rates in confidence buckets (0.55–0.60, 0.60–0.65, etc.)
///    Uses the empirical win rate to dynamically adjust min_confidence:
///    if a bin's actual WR is below breakeven, trades in that bin are skipped.
use crate::config::*;

// ─────────────────────────────────────────────────────────────────
// Brier Score Circuit Breaker
// ─────────────────────────────────────────────────────────────────

/// Rolling Brier score tracker with circuit-breaker logic.
///
/// In the backtest, this processes trades sequentially (time-ordered).
/// In the live bot, call `record()` after each trade resolves and
/// check `is_paused()` before placing the next trade.
pub struct BrierCircuitBreaker {
    /// Ring buffer of (confidence, outcome) pairs
    window: Vec<(f64, f64)>,
    /// Maximum window size for rolling computation
    window_size: usize,
    /// Brier score threshold — above this we pause trading
    threshold: f64,
    /// Current rolling Brier score
    pub current_brier: f64,
    /// Number of trades skipped due to circuit breaker
    pub trades_skipped: usize,
    /// Whether the breaker is currently tripped
    paused: bool,
    /// Brier score needed to un-trip (hysteresis to avoid flapping)
    recovery_threshold: f64,
}

impl BrierCircuitBreaker {
    pub fn new(window_size: usize, threshold: f64) -> Self {
        Self {
            window: Vec::with_capacity(window_size),
            window_size,
            threshold,
            current_brier: 0.0,
            trades_skipped: 0,
            paused: false,
            // Must recover to 80% of trip threshold before re-enabling
            recovery_threshold: threshold * 0.80,
        }
    }

    /// Record a completed trade's predicted confidence and binary outcome.
    /// `confidence` = the model's stated probability of being correct (0.5–1.0)
    /// `outcome` = 1.0 if correct, 0.0 if wrong
    pub fn record(&mut self, confidence: f64, outcome: f64) {
        if self.window.len() >= self.window_size {
            self.window.remove(0);
        }
        self.window.push((confidence, outcome));
        self.recompute();
    }

    /// Recompute the rolling Brier score from the window.
    fn recompute(&mut self) {
        if self.window.is_empty() {
            self.current_brier = 0.0;
            return;
        }
        let sum_sq: f64 = self
            .window
            .iter()
            .map(|&(conf, outcome)| (conf - outcome).powi(2))
            .sum();
        self.current_brier = sum_sq / self.window.len() as f64;

        // Trip the breaker if Brier exceeds threshold
        if self.current_brier > self.threshold {
            self.paused = true;
        }
        // Recover only when Brier drops sufficiently (hysteresis)
        if self.paused && self.current_brier < self.recovery_threshold {
            self.paused = false;
        }
    }

    /// Check if trading should be paused.
    /// Requires at least `window_size / 2` observations before activating.
    pub fn is_paused(&self) -> bool {
        if self.window.len() < self.window_size / 2 {
            return false; // Not enough data to judge
        }
        self.paused
    }

    /// Skip counter for reporting.
    pub fn record_skip(&mut self) {
        self.trades_skipped += 1;
    }

    pub fn window_len(&self) -> usize {
        self.window.len()
    }
}

// ─────────────────────────────────────────────────────────────────
// Confidence-Bin Calibration Table
// ─────────────────────────────────────────────────────────────────

/// One row in the calibration table.
#[derive(Debug, Clone)]
pub struct CalibrationBin {
    /// Lower bound of confidence range (inclusive)
    pub lo: f64,
    /// Upper bound of confidence range (exclusive)
    pub hi: f64,
    /// Number of trades observed in this bin
    pub count: usize,
    /// Number of wins in this bin
    pub wins: usize,
}

impl CalibrationBin {
    pub fn win_rate(&self) -> f64 {
        if self.count == 0 {
            0.0
        } else {
            self.wins as f64 / self.count as f64
        }
    }

    /// Empirical edge = actual_win_rate - breakeven_cost
    /// breakeven_cost ≈ entry_price (0.505 typically)
    pub fn empirical_edge(&self, entry_price: f64) -> f64 {
        self.win_rate() - entry_price
    }
}

/// Tracks per-bin calibration across the backtest.
/// In the live bot, this would be a rolling window per bin.
pub struct CalibrationTable {
    pub bins: Vec<CalibrationBin>,
    /// Minimum trades in a bin before we trust its calibration
    pub min_sample: usize,
    /// The breakeven entry price (for edge calculation)
    pub entry_price: f64,
}

impl CalibrationTable {
    /// Create bins from 0.50 to 1.00 in steps of `bin_width`.
    pub fn new(bin_width: f64, min_sample: usize, entry_price: f64) -> Self {
        let mut bins = Vec::new();
        let mut lo = 0.50;
        while lo < 1.0 - 1e-9 {
            let hi = (lo + bin_width).min(1.0);
            bins.push(CalibrationBin {
                lo,
                hi,
                count: 0,
                wins: 0,
            });
            lo = hi;
        }
        Self {
            bins,
            min_sample,
            entry_price,
        }
    }

    /// Record a trade outcome into the appropriate bin.
    pub fn record(&mut self, confidence: f64, won: bool) {
        for bin in &mut self.bins {
            if confidence >= bin.lo && confidence < bin.hi {
                bin.count += 1;
                if won {
                    bin.wins += 1;
                }
                return;
            }
        }
        // Edge case: confidence == 1.0 goes into the last bin
        if let Some(last) = self.bins.last_mut() {
            if confidence >= last.lo {
                last.count += 1;
                if won {
                    last.wins += 1;
                }
            }
        }
    }

    /// Determine the dynamic minimum confidence based on calibration data.
    /// Returns the lowest bin boundary where the empirical edge is positive
    /// and the bin has enough samples. Falls back to `fallback` if no bin qualifies.
    pub fn dynamic_min_confidence(&self, fallback: f64) -> f64 {
        for bin in &self.bins {
            if bin.count >= self.min_sample && bin.empirical_edge(self.entry_price) > 0.0 {
                return bin.lo;
            }
        }
        fallback
    }

    /// Return the recalibrated confidence for Brier scoring.
    ///
    /// Instead of feeding the model's raw (overconfident) probability to Brier,
    /// substitute the bin's empirical win rate. This way Brier tracks whether
    /// the model is *maintaining its usual edge* rather than whether it's
    /// perfectly calibrated in absolute terms.
    ///
    /// Falls back to raw confidence if the bin has insufficient data.
    pub fn recalibrated_confidence(&self, raw_confidence: f64) -> f64 {
        for bin in &self.bins {
            if raw_confidence >= bin.lo && raw_confidence < bin.hi {
                if bin.count >= self.min_sample {
                    return bin.win_rate();
                }
                return raw_confidence;
            }
        }
        // Edge case: confidence == 1.0 → last bin
        if let Some(last) = self.bins.last() {
            if raw_confidence >= last.lo && last.count >= self.min_sample {
                return last.win_rate();
            }
        }
        raw_confidence
    }

    /// Check if a specific confidence level is in a calibrated-profitable bin.
    /// Used to gate individual trades: even if confidence passes the global
    /// min_confidence, skip if the bin it falls into has negative edge.
    pub fn is_bin_profitable(&self, confidence: f64) -> bool {
        for bin in &self.bins {
            if confidence >= bin.lo && confidence < bin.hi {
                // Not enough data → assume profitable (don't filter prematurely)
                if bin.count < self.min_sample {
                    return true;
                }
                return bin.empirical_edge(self.entry_price) > 0.0;
            }
        }
        true // confidence == 1.0 edge case
    }

    /// Print the calibration table.
    pub fn print_table(&self) {
        println!("\n  Confidence-Bin Calibration Table:");
        println!(
            "  {:>12} {:>7} {:>7} {:>8} {:>10} {:>8}",
            "Bin", "Trades", "Wins", "WR%", "Emp.Edge", "Status"
        );
        println!("  {}", "-".repeat(60));
        for bin in &self.bins {
            if bin.count == 0 {
                continue;
            }
            let wr = bin.win_rate() * 100.0;
            let edge = bin.empirical_edge(self.entry_price);
            let status = if bin.count < self.min_sample {
                "low-n"
            } else if edge > 0.0 {
                "TRADE"
            } else {
                "SKIP"
            };
            println!(
                "  {:>5.2}–{:<5.2} {:>7} {:>7} {:>7.1}% {:>+9.3} {:>8}",
                bin.lo, bin.hi, bin.count, bin.wins, wr, edge, status
            );
        }
    }

    /// Compute overall Brier score across all recorded trades (for final report).
    /// Uses midpoint of each bin as the "predicted probability".
    pub fn overall_brier(&self) -> f64 {
        let mut sum_sq = 0.0;
        let mut total = 0;
        for bin in &self.bins {
            if bin.count == 0 {
                continue;
            }
            let mid = (bin.lo + bin.hi) / 2.0;
            // Each win contributes (mid - 1.0)^2, each loss contributes (mid - 0.0)^2
            let win_contrib = bin.wins as f64 * (mid - 1.0).powi(2);
            let loss_contrib = (bin.count - bin.wins) as f64 * (mid - 0.0).powi(2);
            sum_sq += win_contrib + loss_contrib;
            total += bin.count;
        }
        if total == 0 {
            0.0
        } else {
            sum_sq / total as f64
        }
    }
}

// ─────────────────────────────────────────────────────────────────
// Configuration Constants
// ─────────────────────────────────────────────────────────────────

/// Rolling window size for Brier circuit breaker.
/// 50 trades gives a stable estimate while being responsive.
pub const BRIER_WINDOW_SIZE: usize = 50;

/// Brier score above this trips the circuit breaker.
/// 0.25 = random guessing. We trip at 0.22 — model is degrading.
pub const BRIER_TRIP_THRESHOLD: f64 = 0.22;

/// Width of each confidence bin (5 percentage points).
pub const CALIBRATION_BIN_WIDTH: f64 = 0.05;

/// Minimum trades in a bin before trusting its calibration.
pub const CALIBRATION_MIN_SAMPLE: usize = 15;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_brier_perfect_calibration() {
        let mut breaker = BrierCircuitBreaker::new(10, 0.22);
        // 10 trades at 80% confidence, 8 win → well calibrated
        for _ in 0..8 {
            breaker.record(0.80, 1.0);
        }
        for _ in 0..2 {
            breaker.record(0.80, 0.0);
        }
        // Brier = (8*(0.8-1)^2 + 2*(0.8-0)^2) / 10 = (8*0.04 + 2*0.64)/10 = 1.60/10 = 0.16
        assert!(breaker.current_brier < 0.22);
        assert!(!breaker.is_paused());
    }

    #[test]
    fn test_brier_bad_calibration_trips() {
        let mut breaker = BrierCircuitBreaker::new(10, 0.22);
        // 10 trades at 90% confidence, only 4 win → badly calibrated
        for _ in 0..4 {
            breaker.record(0.90, 1.0);
        }
        for _ in 0..6 {
            breaker.record(0.90, 0.0);
        }
        // Brier = (4*(0.9-1)^2 + 6*(0.9-0)^2) / 10 = (4*0.01 + 6*0.81)/10 = 4.90/10 = 0.49
        assert!(breaker.current_brier > 0.22);
        assert!(breaker.is_paused());
    }

    #[test]
    fn test_calibration_table_bins() {
        let mut table = CalibrationTable::new(0.05, 5, 0.505);
        // Add 10 trades at 0.72 confidence, 8 win
        for _ in 0..8 {
            table.record(0.72, true);
        }
        for _ in 0..2 {
            table.record(0.72, false);
        }
        // Bin [0.70, 0.75): 10 trades, 8 wins, WR=80%, edge=80%-50.5%=29.5%
        let bin = table.bins.iter().find(|b| b.lo == 0.70).unwrap();
        assert_eq!(bin.count, 10);
        assert_eq!(bin.wins, 8);
        assert!(bin.empirical_edge(0.505) > 0.2);
    }

    #[test]
    fn test_dynamic_min_confidence() {
        let mut table = CalibrationTable::new(0.05, 5, 0.505);
        // Bin 0.55-0.60: 20 trades, 9 wins (45% WR) → negative edge
        for _ in 0..9 {
            table.record(0.57, true);
        }
        for _ in 0..11 {
            table.record(0.57, false);
        }
        // Bin 0.60-0.65: 20 trades, 13 wins (65% WR) → positive edge
        for _ in 0..13 {
            table.record(0.62, true);
        }
        for _ in 0..7 {
            table.record(0.62, false);
        }
        let dyn_min = table.dynamic_min_confidence(0.60);
        assert!(dyn_min >= 0.60, "Should skip the 0.55 bin, got {}", dyn_min);
    }
}
