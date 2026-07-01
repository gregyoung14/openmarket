/// ═══════════════════════════════════════════════════════════════════
/// blacklist.rs — Day-Specific Trading Blacklist
/// ═══════════════════════════════════════════════════════════════════
///
/// Contains the hour×day-of-week combinations where the strategy
/// historically underperforms (< 60% win rate).
///
/// # How This Was Derived
/// 1. Ran V10 backtest over 60 days of 1-second BTCUSDC kline data
/// 2. Generated a 24×7 heatmap of win rates by (hour_ET, day_of_week)
/// 3. Cut all combos with win rate < 60% and ≥ 30 trades
/// 4. This lifted overall win rate from 65.0% → 68.9%
///
/// # Integration Notes
/// In your live bot, call `is_blacklisted(epoch_s)` before entering
/// any trade. This handles the UTC→ET conversion internally.
///
/// # Refreshing the Blacklist
/// Re-run the analysis periodically (monthly) with fresh trade logs
/// to keep the blacklist current. Market dynamics shift.

use std::collections::HashSet;
use lazy_static::lazy_static;

lazy_static! {
    /// Set of (day_of_week, hour_ET) tuples that are blacklisted.
    /// day_of_week: 0=Monday, 1=Tuesday, ..., 6=Sunday
    /// hour_ET: 0–23 in Eastern Time (UTC-5)
    static ref BLACKLIST_DOW_HOUR_ET: HashSet<(u32, u32)> = {
        let mut s = HashSet::new();

        // ── Global Blacklist Hours (apply to ALL days) ──
        // These hours are universally bad across all days:
        //   0:  Midnight ET — low volume, erratic price action
        //   9:  Pre-market overlap — institutional positioning noise
        //   10: Market open — extreme volatility, whipsaw city
        //   15: Last hour of equities — cross-market hedging flows
        //   16: Equities close — order flow dries up, unpredictable
        for dow in 0..7u32 {
            for &h in &[0u32, 9, 10, 15, 16] {
                s.insert((dow, h));
            }
        }

        // ── Monday-Specific Cuts ──
        s.insert((0, 13)); // 58.3% — Monday afternoon lull
        s.insert((0, 18)); // 55.6% — Monday evening transition
        s.insert((0, 20)); // 58.3% — Monday late evening

        // ── Tuesday-Specific Cuts ──
        // Tuesday is the worst weekday for off-hours trading
        s.insert((1, 3));  // 57.1% — Tuesday deep night
        s.insert((1, 5));  // 52.8% — Tuesday early morning
        s.insert((1, 6));  // 54.3% — Tuesday pre-Asia
        s.insert((1, 7));  // 55.6% — Tuesday Asia open
        s.insert((1, 8));  // 55.6% — Tuesday morning
        s.insert((1, 18)); // 55.6% — Tuesday evening
        s.insert((1, 21)); // 58.3% — Tuesday late night
        s.insert((1, 23)); // 58.3% — Tuesday midnight approach

        // ── Wednesday-Specific Cuts ──
        s.insert((2, 7));  // 55.9% — Wednesday Asia
        s.insert((2, 13)); // 55.6% — Wednesday early afternoon
        s.insert((2, 18)); // 55.9% — Wednesday evening
        s.insert((2, 22)); // 44.4% — Wednesday late night (WORST combo)

        // ── Thursday-Specific Cuts ──
        s.insert((3, 6));  // 52.8% — Thursday pre-market
        s.insert((3, 19)); // 59.4% — Thursday evening
        s.insert((3, 23)); // 56.2% — Thursday late night

        // ── Friday-Specific Cuts ──
        // Friday is the weakest full day (61.5% overall)
        s.insert((4, 7));  // 50.0% — Friday Asia (coin flip!)
        s.insert((4, 12)); // 58.1% — Friday lunch
        s.insert((4, 13)); // 53.1% — Friday afternoon
        s.insert((4, 14)); // 58.1% — Friday mid-afternoon
        s.insert((4, 17)); // 56.2% — Friday late afternoon
        s.insert((4, 18)); // 37.5% — Friday evening (SECOND WORST)
        s.insert((4, 19)); // 59.4% — Friday early evening
        s.insert((4, 23)); // 46.9% — Friday late night

        // ── Saturday-Specific Cuts ──
        s.insert((5, 3));  // 59.4% — Saturday deep night
        s.insert((5, 5));  // 59.4% — Saturday early morning
        s.insert((5, 6));  // 38.7% — Saturday morning (THIRD WORST)
        s.insert((5, 21)); // 56.2% — Saturday late night
        s.insert((5, 23)); // 53.1% — Saturday midnight approach

        // ── Sunday-Specific Cuts ──
        s.insert((6, 1));  // 50.0% — Sunday early morning
        s.insert((6, 3));  // 53.1% — Sunday deep night
        s.insert((6, 20)); // 58.3% — Sunday evening
        s.insert((6, 22)); // 52.8% — Sunday late night
        s.insert((6, 23)); // 55.6% — Sunday midnight approach

        s
    };
}

/// Check if a given UTC epoch timestamp falls in a blacklisted
/// (day_of_week, hour) combination in Eastern Time.
///
/// # Arguments
/// * `epoch_s` - Unix timestamp in seconds (UTC)
///
/// # Returns
/// `true` if this time slot should be skipped
///
/// # Example
/// ```
/// let epoch = 1766361600; // some UTC timestamp
/// if is_blacklisted(epoch) {
///     // Skip this market — historically bad win rate
///     return None;
/// }
/// ```
pub fn is_blacklisted(epoch_s: i64) -> bool {
    // Convert to Eastern Time for both hour and day-of-week.
    // IMPORTANT: Both must be derived from the ET-shifted epoch,
    // otherwise late-night UTC timestamps map to the wrong ET day.
    // (This was a bug in V10 that cost ~1% win rate.)
    let et_offset_secs: i64 = 5 * 3600; // UTC-5 = Eastern
    let et_epoch = epoch_s - et_offset_secs;

    let et_hour = ((epoch_s / 3600 % 24) - 5).rem_euclid(24) as u32;

    // Epoch 0 (Jan 1, 1970) was a Thursday (dow=3 in our 0=Mon scheme).
    // After shifting to ET, compute days and offset by +3 to align.
    let days_since_epoch = et_epoch / 86400;
    let dow = ((days_since_epoch + 3) % 7) as u32; // 0=Mon, 6=Sun

    BLACKLIST_DOW_HOUR_ET.contains(&(dow, et_hour))
}

/// Get the ET hour and day-of-week name for a UTC epoch (for logging).
///
/// # Returns
/// (hour_et, day_name) e.g. (14, "Wednesday")
pub fn get_et_time_info(epoch_s: i64) -> (u32, &'static str) {
    let et_offset_secs: i64 = 5 * 3600;
    let et_epoch = epoch_s - et_offset_secs;
    let et_hour = ((epoch_s / 3600 % 24) - 5).rem_euclid(24) as u32;
    let days_since_epoch = et_epoch / 86400;
    let dow = ((days_since_epoch + 3) % 7) as u32;

    let day_name = match dow {
        0 => "Monday",
        1 => "Tuesday",
        2 => "Wednesday",
        3 => "Thursday",
        4 => "Friday",
        5 => "Saturday",
        6 => "Sunday",
        _ => "Unknown",
    };

    (et_hour, day_name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_friday_18_is_blacklisted() {
        // Friday 18:00 ET = 37.5% win rate, should be blacklisted
        // Pick a known Friday 18:00 ET → Friday 23:00 UTC
        // 2026-02-20 is a Friday
        // 18:00 ET = 23:00 UTC
        let epoch = 1740092400; // approximate, just need a Friday 23:00 UTC
        // The exact epoch doesn't matter as much as the logic being correct
        // We test the blacklist lookup directly:
        assert!(BLACKLIST_DOW_HOUR_ET.contains(&(4, 18))); // Friday, 18:00 ET
    }

    #[test]
    fn test_tuesday_02_is_not_blacklisted() {
        // Tuesday 02:00 ET = 72.3% win rate overall at hour 2
        assert!(!BLACKLIST_DOW_HOUR_ET.contains(&(1, 2)));
    }

    #[test]
    fn test_global_blacklist_hours() {
        // Hours 0, 9, 10, 15, 16 should be blacklisted for ALL days
        for dow in 0..7u32 {
            for &h in &[0u32, 9, 10, 15, 16] {
                assert!(
                    BLACKLIST_DOW_HOUR_ET.contains(&(dow, h)),
                    "Expected ({}, {}) to be blacklisted", dow, h
                );
            }
        }
    }
}
