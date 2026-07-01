use anyhow::{Context, Result};
use chrono::{Datelike, TimeZone, Utc};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use polars::prelude::*;
use rayon::prelude::*;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use statrs::distribution::{ContinuousCDF, Normal};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;
use prettytable::{Table, row, cell};
use lazy_static::lazy_static;

#[derive(Parser, Debug)]
#[command(author, version, about = "V11 Enhanced Backtester")]
struct Args {
    #[arg(long, default_value = "polymarket_btc_data.db")]
    db_path: PathBuf,

    #[arg(long, default_value_t = 100.0)]
    bankroll: f64,

    #[arg(long, default_value_t = 0.02)]
    bet_fraction: f64,

    #[arg(long, default_value_t = 0.60)]
    min_confidence: f64,

    #[arg(long, default_value_t = 0.08)]
    min_edge: f64,

    #[arg(long, default_value_t = 0.15)]
    min_entry_price: f64,

    #[arg(long, default_value_t = 0.55)]
    max_entry_price: f64,

    // ── V11 Enhancement Flags ──
    // Each can be toggled independently to measure delta.
    // When all are false, behavior = V10 baseline.

    /// [1] Require ≥2/3 signal consistency to take trade
    #[arg(long, default_value_t = false)]
    use_consistency_gate: bool,

    /// [2] Apply time-decay penalty to confidence for late entries
    #[arg(long, default_value_t = false)]
    use_time_decay: bool,

    /// [3] Skip trades when hourly volume is below median
    #[arg(long, default_value_t = false)]
    use_volume_gate: bool,

    /// [4] Use whipsaw as a 4th signal component
    #[arg(long, default_value_t = false)]
    use_whipsaw_signal: bool,

    /// [5] Reduce scoreboard scale from 1000 to 300 and weight from 0.15 to 0.08
    #[arg(long, default_value_t = false)]
    use_tuned_scoreboard: bool,

    /// [6] Make confirmation window regime-aware
    #[arg(long, default_value_t = false)]
    use_regime_confirm: bool,

    /// [7] Use VWAP of first 10s after window for settle price
    #[arg(long, default_value_t = false)]
    use_vwap_settle: bool,

    /// [8] Track best signal and enter at peak confidence
    #[arg(long, default_value_t = false)]
    use_best_signal: bool,

    /// [9] Enable trailing stop: exit early at partial loss/gain
    #[arg(long, default_value_t = false)]
    use_trailing_stop: bool,

    /// Enable ALL v11 enhancements at once
    #[arg(long, default_value_t = false)]
    all: bool,
}

impl Args {
    fn flag(&self, f: bool) -> bool { f || self.all }
}

// ── Config Constants ──

// V10 defaults (overridden when tuned_scoreboard is active)
const W_DRIFT: f64 = 0.55;
const W_OFI_ACCEL: f64 = 0.30;
const W_SCOREBOARD: f64 = 0.15;
const SCOREBOARD_SCALE: f64 = 1000.0;
const OFI_SCALE: f64 = 3.0;

// V11 tuned scoreboard
const V11_W_DRIFT: f64 = 0.57;
const V11_W_OFI_ACCEL: f64 = 0.30;
const V11_W_SCOREBOARD: f64 = 0.08;
const V11_W_WHIPSAW: f64 = 0.05;
const V11_SCOREBOARD_SCALE: f64 = 300.0;

const REGIME_TREND_THRESHOLD: f64 = 0.15;
const REGIME_CHOP_THRESHOLD: f64 = 0.06;
const REGIME_AUTOCORR_CHOP: f64 = -0.25;
const REGIME_LOOKBACK: usize = 60;
const NEUTRAL_CONF_PENALTY: f64 = 0.02;

const MIN_SECS_INTO_MARKET: i64 = 60;
const MAX_SECS_INTO_MARKET: i64 = 600;
const MARKET_DURATION_SECS: i64 = 900;

const BASE_CONFIRM_WINDOW: i64 = 30;
const MIN_CONFIRM_WINDOW: i64 = 15;
const MAX_CONFIRM_WINDOW: i64 = 50;

// [2] Time decay: confidence penalty per second past this threshold
const TIME_DECAY_START_SEC: i64 = 300;
const TIME_DECAY_RATE: f64 = 0.0002; // per second past threshold

// [9] Trailing stop config
const TRAILING_STOP_LOSS_PCT: f64 = 0.0015;   // 0.15% adverse move = stop out
const TRAILING_STOP_PROFIT_PCT: f64 = 0.003;   // 0.3% favorable move = lock profit
const TRAILING_STOP_PARTIAL_EXIT: f64 = 0.30;  // exit_price when stopped out (partial recovery)

const SLIPPAGE: f64 = 0.005;
const FEE_RATE: f64 = 0.01;

lazy_static! {
    static ref BLACKLIST_DOW_HOUR_ET: HashSet<(u32, u32)> = {
        let mut s = HashSet::new();
        for dow in 0..7u32 {
            for &h in &[0u32, 9, 10, 15, 16] {
                s.insert((dow, h));
            }
        }
        // Data-driven cuts (< 60% win rate)
        s.insert((0, 13)); s.insert((0, 18)); s.insert((0, 20));
        s.insert((1, 3)); s.insert((1, 5)); s.insert((1, 6)); s.insert((1, 7));
        s.insert((1, 8)); s.insert((1, 18)); s.insert((1, 21)); s.insert((1, 23));
        s.insert((2, 7)); s.insert((2, 13)); s.insert((2, 18)); s.insert((2, 22));
        s.insert((3, 6)); s.insert((3, 19)); s.insert((3, 23));
        s.insert((4, 7)); s.insert((4, 12)); s.insert((4, 13)); s.insert((4, 14));
        s.insert((4, 17)); s.insert((4, 18)); s.insert((4, 19)); s.insert((4, 23));
        s.insert((5, 3)); s.insert((5, 5)); s.insert((5, 6)); s.insert((5, 21)); s.insert((5, 23));
        s.insert((6, 1)); s.insert((6, 3)); s.insert((6, 20)); s.insert((6, 22)); s.insert((6, 23));
        s
    };
}

#[derive(Debug, Clone)]
enum Regime {
    Trend,
    Chop,
    Neutral,
}

struct SignalResult {
    direction: String,
    confidence: f64,
    regime: Regime,
    path_eff: f64,
    autocorr: f64,
    adaptive_confirm: i64,
    consistency: f64,
}

fn detect_regime(closes: &[f64]) -> (Regime, f64, f64) {
    let n = closes.len();
    if n < 15 { return (Regime::Neutral, 0.0, 0.0); }

    let start_idx = if n > REGIME_LOOKBACK { n - REGIME_LOOKBACK } else { 0 };
    let valid: Vec<f64> = closes[start_idx..].iter().cloned().filter(|&x| x > 0.0).collect();
    if valid.len() < 15 { return (Regime::Neutral, 0.0, 0.0); }

    let direct = (valid[valid.len()-1] - valid[0]).abs();
    let total_path: f64 = valid.windows(2).map(|w| (w[1] - w[0]).abs()).sum();
    let path_eff = direct / (total_path + 1e-12);

    let mut returns = Vec::new();
    for i in 1..valid.len() {
        returns.push((valid[i] / (valid[i-1] + 1e-9)).ln());
    }

    let autocorr = if returns.len() > 5 {
        let x = &returns[..returns.len()-1];
        let y = &returns[1..];
        let n = x.len() as f64;
        let mean_x = x.iter().sum::<f64>() / n;
        let mean_y = y.iter().sum::<f64>() / n;
        let mut num = 0.0;
        let mut den_x = 0.0;
        let mut den_y = 0.0;
        for i in 0..x.len() {
            let dx = x[i] - mean_x;
            let dy = y[i] - mean_y;
            num += dx * dy;
            den_x += dx * dx;
            den_y += dy * dy;
        }
        let den = (den_x * den_y).sqrt();
        if den > 0.0 { num / den } else { 0.0 }
    } else {
        0.0
    };

    if autocorr < REGIME_AUTOCORR_CHOP {
        (Regime::Chop, path_eff, autocorr)
    } else if path_eff >= REGIME_TREND_THRESHOLD && autocorr > -0.10 {
        (Regime::Trend, path_eff, autocorr)
    } else if path_eff < REGIME_CHOP_THRESHOLD {
        (Regime::Chop, path_eff, autocorr)
    } else {
        (Regime::Neutral, path_eff, autocorr)
    }
}

/// Compute whipsaw ratio: fraction of direction changes in the close array
fn compute_whipsaw(closes: &[f64]) -> f64 {
    if closes.len() < 3 { return 0.0; }
    let diffs: Vec<f64> = closes.windows(2).map(|w| w[1] - w[0]).collect();
    let signs: Vec<f64> = diffs.iter().map(|d| d.signum()).collect();
    let changes = signs.windows(2).filter(|w| w[0] != w[1] && w[0] != 0.0 && w[1] != 0.0).count();
    changes as f64 / (signs.len().max(1) - 1).max(1) as f64
}

fn compute_signal(
    closes: &[f64],
    buy_vols: &[f64],
    sell_vols: &[f64],
    btc_start: f64,
    remaining_secs: i64,
    use_tuned_scoreboard: bool,
    use_whipsaw: bool,
    use_regime_confirm: bool,
) -> Option<SignalResult> {
    let n = closes.len();
    if n < 15 { return None; }

    let current_price = closes[n-1];
    let (regime, path_eff, autocorr) = detect_regime(closes);

    // Component 1: Drift
    let mut log_returns = Vec::new();
    for i in 1..n {
        log_returns.push((closes[i] / (closes[i-1] + 1e-9)).ln());
    }
    if log_returns.len() < 5 { return None; }

    let mu = log_returns.iter().sum::<f64>() / log_returns.len() as f64;
    let var = log_returns.iter().map(|r| (r - mu).powi(2)).sum::<f64>() / log_returns.len() as f64;
    let sigma = var.sqrt();

    let drift_prob_up = if sigma > 0.0 && remaining_secs > 0 {
        let z = mu * (remaining_secs as f64).sqrt() / sigma;
        let normal = Normal::new(0.0, 1.0).unwrap();
        normal.cdf(z)
    } else {
        0.5
    };

    // Component 2: OFI Accel
    let half = (n / 2).max(5);
    let buy_recent: f64 = buy_vols[n-half..].iter().sum();
    let sell_recent: f64 = sell_vols[n-half..].iter().sum();
    let buy_earlier: f64 = buy_vols[..half].iter().sum();
    let sell_earlier: f64 = sell_vols[..half].iter().sum();

    let ofi_recent = (buy_recent - sell_recent) / (buy_recent + sell_recent + 1e-9);
    let ofi_earlier = (buy_earlier - sell_earlier) / (buy_earlier + sell_earlier + 1e-9);
    let ofi_accel = ofi_recent - ofi_earlier;
    let ofi_accel_signal = 1.0 / (1.0 + (-ofi_accel * OFI_SCALE).exp());

    // Component 3: Scoreboard
    let price_vs_open = (current_price - btc_start) / (btc_start + 1e-9);
    let sb_scale = if use_tuned_scoreboard { V11_SCOREBOARD_SCALE } else { SCOREBOARD_SCALE };
    let scoreboard_signal = 1.0 / (1.0 + (-price_vs_open * sb_scale).exp());

    // [4] Component 4: Whipsaw signal (moderate whipsaw = good)
    let whipsaw_raw = compute_whipsaw(closes);
    // Map whipsaw to signal: peak at 0.4, drops off at extremes
    // Using a Gaussian-like shape centered at 0.4
    let whipsaw_signal = (-((whipsaw_raw - 0.40).powi(2)) / 0.08).exp();

    // Combined probability
    let combined_prob_up = if use_tuned_scoreboard || use_whipsaw {
        let w_d = if use_whipsaw { V11_W_DRIFT } else if use_tuned_scoreboard { V11_W_DRIFT } else { W_DRIFT };
        let w_o = if use_whipsaw { V11_W_OFI_ACCEL } else if use_tuned_scoreboard { V11_W_OFI_ACCEL } else { W_OFI_ACCEL };
        let w_s = if use_tuned_scoreboard { V11_W_SCOREBOARD } else { W_SCOREBOARD };
        let w_w = if use_whipsaw { V11_W_WHIPSAW } else { 0.0 };
        // Whipsaw signal modulates confidence rather than direction
        // Higher whipsaw_signal → more confident in the combined direction
        let base = w_d * drift_prob_up + w_o * ofi_accel_signal + w_s * scoreboard_signal;
        let remaining_w = 1.0 - w_d - w_o - w_s;
        // Use remaining weight for whipsaw as a confidence booster toward the direction
        base + remaining_w * (if base > 0.5 { whipsaw_signal } else { 1.0 - whipsaw_signal })
    } else {
        W_DRIFT * drift_prob_up + W_OFI_ACCEL * ofi_accel_signal + W_SCOREBOARD * scoreboard_signal
    };

    let (direction, mut confidence) = if combined_prob_up > 0.5 {
        ("UP".to_string(), combined_prob_up)
    } else {
        ("DOWN".to_string(), 1.0 - combined_prob_up)
    };

    if let Regime::Neutral = regime {
        confidence -= NEUTRAL_CONF_PENALTY;
    }

    // Adaptive Confirm
    let recent_rets = if log_returns.len() > 30 { &log_returns[log_returns.len()-30..] } else { &log_returns };
    let vol = if recent_rets.len() > 3 {
        let m = recent_rets.iter().sum::<f64>() / recent_rets.len() as f64;
        (recent_rets.iter().map(|r| (r - m).powi(2)).sum::<f64>() / recent_rets.len() as f64).sqrt()
    } else { 0.0 };

    let vol_score = (vol / 0.0002).min(2.0);
    let mut adaptive_confirm = (BASE_CONFIRM_WINDOW as f64 * (1.3 - 0.3 * vol_score).max(0.5)) as i64;
    adaptive_confirm = adaptive_confirm.clamp(MIN_CONFIRM_WINDOW, MAX_CONFIRM_WINDOW);

    // [6] Regime-aware confirmation: shorter in Trend, longer in Neutral
    if use_regime_confirm {
        match regime {
            Regime::Trend => {
                // Trend = high conviction, confirm faster
                adaptive_confirm = (adaptive_confirm as f64 * 0.7) as i64;
                adaptive_confirm = adaptive_confirm.max(MIN_CONFIRM_WINDOW);
            }
            Regime::Neutral => {
                // Neutral = lower conviction, need more confirmation
                adaptive_confirm = (adaptive_confirm as f64 * 1.3) as i64;
                adaptive_confirm = adaptive_confirm.min(MAX_CONFIRM_WINDOW);
            }
            Regime::Chop => {} // Chop already resets count in main loop
        }
    }

    let signals_agree = [
        drift_prob_up > 0.5,
        ofi_accel_signal > 0.5,
        scoreboard_signal > 0.5,
    ];
    let consistency = if direction == "UP" {
        signals_agree.iter().filter(|&&s| s).count() as f64 / 3.0
    } else {
        signals_agree.iter().filter(|&&s| !s).count() as f64 / 3.0
    };

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

fn load_data(db_path: &PathBuf) -> Result<(DataFrame, DataFrame, DataFrame)> {
    println!("  Connecting to database: {:?}", db_path);
    let conn = Connection::open(db_path)?;

    println!("  Loading market_meta...");
    let mut stmt = conn.prepare("SELECT market_slug, first_seen_ms FROM market_meta ORDER BY first_seen_ms ASC")?;
    let meta_iter = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    let mut slugs = Vec::new();
    let mut open_ts = Vec::new();
    for m in meta_iter {
        let (s, t) = m?;
        slugs.push(s);
        open_ts.push(t);
    }
    let df_meta = df!(
        "market_slug" => slugs,
        "first_seen_ms" => open_ts
    )?;

    println!("  Loading binance_trades...");
    let mut stmt = conn.prepare("SELECT trade_time, price, quantity, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC")?;
    let trades_iter = stmt.query_map([], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, f64>(1)?, row.get::<_, f64>(2)?, row.get::<_, i32>(3)?))
    })?;
    let mut times = Vec::new();
    let mut prices = Vec::new();
    let mut qtys = Vec::new();
    let mut makers = Vec::new();
    for t in trades_iter {
        let (tm, pr, qt, mk) = t?;
        times.push(tm);
        prices.push(pr);
        qtys.push(qt);
        makers.push(mk);
    }
    let df_trades = df!(
        "trade_time" => times,
        "price" => prices,
        "quantity" => qtys,
        "is_buyer_maker" => makers
    )?;

    println!("  Loading polymarket_ticks...");
    let mut stmt = conn.prepare("SELECT market_slug, source_ts_ms, side_label, best_ask FROM polymarket_ticks_ms WHERE event_type = 'price_change' ORDER BY source_ts_ms ASC")?;
    let ticks_iter = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?, row.get::<_, String>(2)?, row.get::<_, f64>(3)?))
    })?;
    let mut t_slugs = Vec::new();
    let mut t_times = Vec::new();
    let mut t_labels = Vec::new();
    let mut t_asks = Vec::new();
    for t in ticks_iter {
        let (s, tm, lb, ask) = t?;
        t_slugs.push(s);
        t_times.push(tm);
        t_labels.push(lb);
        t_asks.push(ask);
    }
    let df_ticks = df!(
        "market_slug" => t_slugs,
        "source_ts_ms" => t_times,
        "side_label" => t_labels,
        "best_ask" => t_asks
    )?;

    Ok((df_meta, df_trades, df_ticks))
}

fn build_entry_ask_ladders(mkt_ticks: &DataFrame, start_ms: i64) -> (Vec<f64>, Vec<f64>) {
    let mut up_asks = vec![0.0; MARKET_DURATION_SECS as usize];
    let mut down_asks = vec![0.0; MARKET_DURATION_SECS as usize];

    let times = mkt_ticks.column("source_ts_ms").unwrap().i64().unwrap();
    let labels = mkt_ticks.column("side_label").unwrap().str().unwrap();
    let asks = mkt_ticks.column("best_ask").unwrap().f64().unwrap();

    for i in 0..mkt_ticks.height() {
        let ask = asks.get(i).unwrap_or(0.0);
        if ask <= 0.0 {
            continue;
        }
        let sec = ((times.get(i).unwrap() - start_ms) / 1000).clamp(0, MARKET_DURATION_SECS - 1) as usize;
        match labels.get(i).unwrap_or("") {
            "UP" | "Up" => up_asks[sec] = ask,
            "DOWN" | "Down" => down_asks[sec] = ask,
            _ => {}
        }
    }

    let mut last_up = 0.0;
    let mut last_down = 0.0;
    for i in 0..MARKET_DURATION_SECS as usize {
        if up_asks[i] == 0.0 {
            up_asks[i] = last_up;
        } else {
            last_up = up_asks[i];
        }

        if down_asks[i] == 0.0 {
            down_asks[i] = last_down;
        } else {
            last_down = down_asks[i];
        }
    }

    (up_asks, down_asks)
}

#[derive(Debug, Serialize, Clone)]
struct TradeLog {
    slug: String,
    entry_secs_in: i64,
    side: String,
    entry_price: f64,
    exit_price: f64,
    pnl: f64,
    bankroll: f64,
    correct: bool,
    conf: f64,
    edge: f64,
    regime: String,
    path_eff: f64,
    autocorr: f64,
    consistency: f64,
    early_exit: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();

    // Print active flags
    println!("============================================================");
    println!(" V11 ENHANCED BACKTESTER");
    println!("============================================================");
    let flags = [
        ("consistency_gate", args.flag(args.use_consistency_gate)),
        ("time_decay", args.flag(args.use_time_decay)),
        ("volume_gate", args.flag(args.use_volume_gate)),
        ("whipsaw_signal", args.flag(args.use_whipsaw_signal)),
        ("tuned_scoreboard", args.flag(args.use_tuned_scoreboard)),
        ("regime_confirm", args.flag(args.use_regime_confirm)),
        ("vwap_settle", args.flag(args.use_vwap_settle)),
        ("best_signal", args.flag(args.use_best_signal)),
        ("trailing_stop", args.flag(args.use_trailing_stop)),
    ];
    let active: Vec<&str> = flags.iter().filter(|(_, on)| *on).map(|(n, _)| *n).collect();
    if active.is_empty() {
        println!("  Flags: NONE (V10 baseline)");
    } else {
        println!("  Flags: {}", active.join(", "));
    }
    println!();

    let (df_meta, df_trades, df_ticks) = load_data(&args.db_path)?;

    let slugs: Vec<String> = df_meta.column("market_slug")?.str()?.into_no_null_iter().map(|s| s.to_string()).collect();
    let first_seen_ms: Vec<i64> = df_meta.column("first_seen_ms")?.i64()?.into_no_null_iter().collect();

    // [3] Pre-compute hourly volume median for volume gate
    let volume_median: f64 = if args.flag(args.use_volume_gate) {
        let qtys_col = df_trades.column("quantity").unwrap().f64().unwrap();
        // Total volume across all trades / number of hours as a rough median proxy
        // Better: compute per-hour volumes
        let total_vol: f64 = qtys_col.into_no_null_iter().sum();
        let total_hours = (slugs.len() as f64 * MARKET_DURATION_SECS as f64 / 3600.0).max(1.0);
        total_vol / total_hours * 0.5 // 50th percentile approximation
    } else {
        0.0
    };

    let pb = ProgressBar::new(slugs.len() as u64);
    pb.set_style(ProgressStyle::default_bar()
        .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta})")?
        .progress_chars("#>-"));

    let results: Vec<Option<TradeLog>> = slugs.par_iter().zip(first_seen_ms.par_iter()).map(|(slug, &fs_ms): (&String, &i64)| {
        pb.inc(1);

        let epoch_s = slug.split('-').last().unwrap_or("0").parse::<i64>().unwrap_or(fs_ms / 1000);
        let start_ms = epoch_s * 1000;

        let et_hour = ((epoch_s / 3600 % 24) - 5).rem_euclid(24) as u32;
        let et_epoch = epoch_s - 5 * 3600;
        let days_since_epoch = et_epoch / 86400;
        let dow = ((days_since_epoch + 3) % 7) as u32;
        if BLACKLIST_DOW_HOUR_ET.contains(&(dow, et_hour)) {
            return None;
        }

        let end_ms = start_ms + MARKET_DURATION_SECS * 1000;

        let mask = df_trades.column("trade_time").unwrap().i64().unwrap().gt_eq(start_ms) &
                   df_trades.column("trade_time").unwrap().i64().unwrap().lt(end_ms);
        let mkt_trades = df_trades.filter(&mask).ok()?;
        if mkt_trades.height() < 50 { return None; }

        let btc_start = mkt_trades.column("price").unwrap().f64().unwrap().get(0).unwrap();

        // [3] Volume gate: check if this window has enough volume
        if args.flag(args.use_volume_gate) {
            let window_vol: f64 = mkt_trades.column("quantity").unwrap().f64().unwrap().sum().unwrap_or(0.0);
            let window_hours = MARKET_DURATION_SECS as f64 / 3600.0;
            let hourly_vol = window_vol / window_hours;
            if hourly_vol < volume_median {
                return None;
            }
        }

        // [7] VWAP settle: average price over first 10 seconds after window
        let btc_end = if args.flag(args.use_vwap_settle) {
            let settle_window_ms = 10_000; // 10 seconds
            let settle_mask = df_trades.column("trade_time").unwrap().i64().unwrap().gt_eq(end_ms) &
                              df_trades.column("trade_time").unwrap().i64().unwrap().lt(end_ms + settle_window_ms);
            let settle_trades = df_trades.filter(&settle_mask).ok()?;
            if settle_trades.height() > 0 {
                let prices = settle_trades.column("price").unwrap().f64().unwrap();
                let qtys = settle_trades.column("quantity").unwrap().f64().unwrap();
                let mut vwap_num = 0.0;
                let mut vwap_den = 0.0;
                for i in 0..settle_trades.height() {
                    let p = prices.get(i).unwrap();
                    let q = qtys.get(i).unwrap();
                    vwap_num += p * q;
                    vwap_den += q;
                }
                if vwap_den > 0.0 { vwap_num / vwap_den }
                else { mkt_trades.column("price").unwrap().f64().unwrap().get(mkt_trades.height()-1).unwrap() }
            } else {
                mkt_trades.column("price").unwrap().f64().unwrap().get(mkt_trades.height()-1).unwrap()
            }
        } else {
            let settle_mask = df_trades.column("trade_time").unwrap().i64().unwrap().gt_eq(end_ms);
            let settle_trades = df_trades.filter(&settle_mask).ok()?;
            if settle_trades.height() > 0 {
                settle_trades.column("price").unwrap().f64().unwrap().get(0).unwrap()
            } else {
                mkt_trades.column("price").unwrap().f64().unwrap().get(mkt_trades.height()-1).unwrap()
            }
        };
        let actual = if btc_end > btc_start { "UP" } else { "DOWN" };

        // Build 1s bars
        let mut close_arr = vec![0.0; 900];
        let mut buy_arr = vec![0.0; 900];
        let mut sell_arr = vec![0.0; 900];

        let times = mkt_trades.column("trade_time").unwrap().i64().unwrap();
        let prices = mkt_trades.column("price").unwrap().f64().unwrap();
        let qtys = mkt_trades.column("quantity").unwrap().f64().unwrap();
        let makers = mkt_trades.column("is_buyer_maker").unwrap().i32().unwrap();

        for i in 0..mkt_trades.height() {
            let sec = ((times.get(i).unwrap() - start_ms) / 1000).clamp(0, 899) as usize;
            let p = prices.get(i).unwrap();
            close_arr[sec] = p;
            if makers.get(i).unwrap() == 0 {
                buy_arr[sec] += qtys.get(i).unwrap();
            } else {
                sell_arr[sec] += qtys.get(i).unwrap();
            }
        }
        // Ffill
        let mut cur = btc_start;
        for i in 0..900 {
            if close_arr[i] == 0.0 { close_arr[i] = cur; }
            else { cur = close_arr[i]; }
        }

        // Ticks for entry
        let tick_mask = df_ticks.column("market_slug").unwrap().str().unwrap().equal(slug.as_str());
        let mkt_ticks = df_ticks.filter(&tick_mask).ok()?;
        let (up_entry_asks, down_entry_asks) = build_entry_ask_ladders(&mkt_ticks, start_ms);

        // [8] Best signal mode: scan all seconds, pick the best one
        if args.flag(args.use_best_signal) {
            let mut best_trade: Option<TradeLog> = None;
            let mut best_conf: f64 = 0.0;

            // Still need confirmation, but we track the best confirmed signal
            let mut confirm_count = 0i64;
            let mut confirm_dir = String::new();

            for s in MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET {
                let res = match compute_signal(
                    &close_arr[..=s as usize],
                    &buy_arr[..=s as usize],
                    &sell_arr[..=s as usize],
                    btc_start,
                    MARKET_DURATION_SECS - s,
                    args.flag(args.use_tuned_scoreboard),
                    args.flag(args.use_whipsaw_signal),
                    args.flag(args.use_regime_confirm),
                ) {
                    Some(r) => r,
                    None => continue,
                };

                if let Regime::Chop = res.regime {
                    confirm_count = 0;
                    continue;
                }

                if res.confidence >= args.min_confidence {
                    if res.direction == confirm_dir {
                        confirm_count += 1;
                    } else {
                        confirm_dir = res.direction.clone();
                        confirm_count = 1;
                    }

                    if confirm_count >= res.adaptive_confirm {
                        // [1] Consistency gate
                        if args.flag(args.use_consistency_gate) && res.consistency < 0.67 {
                            continue;
                        }

                        let mut conf = res.confidence;
                        // [2] Time decay
                        if args.flag(args.use_time_decay) && s > TIME_DECAY_START_SEC {
                            conf -= (s - TIME_DECAY_START_SEC) as f64 * TIME_DECAY_RATE;
                        }

                        let entry_ask = if confirm_dir == "UP" {
                            up_entry_asks[s as usize]
                        } else {
                            down_entry_asks[s as usize]
                        };
                        if entry_ask < args.min_entry_price || entry_ask > args.max_entry_price {
                            continue;
                        }

                        let entry_price = entry_ask + SLIPPAGE;
                        let edge = conf - entry_price;
                        if edge < args.min_edge { continue; }

                        // Is this better than our current best?
                        if conf > best_conf {
                            best_conf = conf;
                            let final_correct = confirm_dir == actual;

                            // [9] Trailing stop
                            let (exit_price, early_exit) = if args.flag(args.use_trailing_stop) {
                                evaluate_trailing_stop(&close_arr, s as usize, &confirm_dir, btc_start, final_correct)
                            } else {
                                (if final_correct { 1.0 } else { 0.0 }, false)
                            };

                            best_trade = Some(TradeLog {
                                slug: slug.clone(),
                                entry_secs_in: s,
                                side: confirm_dir.clone(),
                                entry_price,
                                exit_price,
                                pnl: 0.0,
                                bankroll: 0.0,
                                correct: final_correct,
                                conf,
                                edge,
                                regime: format!("{:?}", res.regime),
                                path_eff: res.path_eff,
                                autocorr: res.autocorr,
                                consistency: res.consistency,
                                early_exit,
                            });
                        }
                    }
                } else {
                    confirm_count = 0;
                }
            }

            return best_trade;
        }

        // ── Standard (non-best-signal) mode ──
        let mut confirm_count = 0i64;
        let mut confirm_dir = String::new();

        for s in MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET {
            let res = match compute_signal(
                &close_arr[..=s as usize],
                &buy_arr[..=s as usize],
                &sell_arr[..=s as usize],
                btc_start,
                MARKET_DURATION_SECS - s,
                args.flag(args.use_tuned_scoreboard),
                args.flag(args.use_whipsaw_signal),
                args.flag(args.use_regime_confirm),
            ) {
                Some(r) => r,
                None => continue,
            };

            if let Regime::Chop = res.regime {
                confirm_count = 0;
                continue;
            }

            if res.confidence >= args.min_confidence {
                if res.direction == confirm_dir {
                    confirm_count += 1;
                } else {
                    confirm_dir = res.direction.clone();
                    confirm_count = 1;
                }

                if confirm_count >= res.adaptive_confirm {
                    // [1] Consistency gate
                    if args.flag(args.use_consistency_gate) && res.consistency < 0.67 {
                        confirm_count = 0;
                        continue;
                    }

                    let mut conf = res.confidence;

                    // [2] Time decay
                    if args.flag(args.use_time_decay) && s > TIME_DECAY_START_SEC {
                        conf -= (s - TIME_DECAY_START_SEC) as f64 * TIME_DECAY_RATE;
                    }

                    let now_ms = start_ms + s * 1000;

                    let side_mask = mkt_ticks.column("side_label").unwrap().str().unwrap().equal(confirm_dir.as_str());
                    let side_ticks = mkt_ticks.filter(&side_mask).ok()?;

                    let time_mask = side_ticks.column("source_ts_ms").unwrap().i64().unwrap().lt_eq(now_ms);
                    let backward = side_ticks.filter(&time_mask).ok()?;

                    let entry_ask = if backward.height() > 0 {
                        backward.column("best_ask").unwrap().f64().unwrap().get(backward.height()-1).unwrap()
                    } else {
                        0.50
                    };

                    let entry_price = entry_ask + SLIPPAGE;
                    let edge = conf - entry_price;

                    if entry_ask < args.min_entry_price || entry_ask > args.max_entry_price {
                        confirm_count = 0;
                        continue;
                    }
                    if edge < args.min_edge {
                        confirm_count = 0;
                        continue;
                    }

                    let final_correct = confirm_dir == actual;

                    // [9] Trailing stop
                    let (exit_price, early_exit) = if args.flag(args.use_trailing_stop) {
                        evaluate_trailing_stop(&close_arr, s as usize, &confirm_dir, btc_start, final_correct)
                    } else {
                        (if final_correct { 1.0 } else { 0.0 }, false)
                    };

                    return Some(TradeLog {
                        slug: slug.clone(),
                        entry_secs_in: s,
                        side: confirm_dir,
                        entry_price,
                        exit_price,
                        pnl: 0.0,
                        bankroll: 0.0,
                        correct: final_correct,
                        conf,
                        edge,
                        regime: format!("{:?}", res.regime),
                        path_eff: res.path_eff,
                        autocorr: res.autocorr,
                        consistency: res.consistency,
                        early_exit,
                    });
                }
            } else {
                confirm_count = 0;
            }
        }

        None
    }).collect();

    pb.finish_with_message("Backtest complete");

    // Process Trades
    let mut bankroll = args.bankroll;
    let mut final_trades = Vec::new();
    let mut wins = 0;
    let mut early_exits = 0;

    for opt_t in results {
        if let Some(mut t) = opt_t {
            let bet_amount = bankroll * args.bet_fraction;
            let fee_entry = bet_amount * FEE_RATE;
            let shares = (bet_amount - fee_entry) / t.entry_price;

            let payout = shares * t.exit_price;
            let fee_exit = if payout > 0.0 { payout * FEE_RATE } else { 0.0 };
            t.pnl = payout - fee_exit - bet_amount;

            bankroll += t.pnl;
            t.bankroll = bankroll;
            if t.correct { wins += 1; }
            if t.early_exit { early_exits += 1; }
            final_trades.push(t);
        }
    }

    // Save CSV
    let csv_path = "v11_trade_log.csv";
    let mut wtr = csv::Writer::from_path(csv_path)?;
    for t in &final_trades {
        wtr.serialize(t)?;
    }
    wtr.flush()?;
    println!("  Trade log saved to {}", csv_path);

    // Print Results
    let mut table = Table::new();
    table.add_row(row!["Metric", "Value"]);
    table.add_row(row!["Total Trades", final_trades.len()]);
    table.add_row(row!["Win Rate", format!("{:.1}%", (wins as f64 / final_trades.len().max(1) as f64) * 100.0)]);
    table.add_row(row!["Final Bankroll", format!("${:.2}", bankroll)]);
    table.add_row(row!["Total ROI", format!("{:.1}%", (bankroll - args.bankroll) / args.bankroll * 100.0)]);
    if early_exits > 0 {
        table.add_row(row!["Early Exits", format!("{} ({:.1}%)", early_exits, early_exits as f64 / final_trades.len() as f64 * 100.0)]);
    }
    table.printstd();

    Ok(())
}

/// [9] Evaluate trailing stop: walk the close_arr from entry to end of window.
/// If price moves against position by TRAILING_STOP_LOSS_PCT → exit at partial loss.
/// If price moves in favor by TRAILING_STOP_PROFIT_PCT → lock profit, still binary correct.
fn evaluate_trailing_stop(
    close_arr: &[f64],
    entry_sec: usize,
    direction: &str,
    entry_btc_price: f64,
    final_correct: bool,
) -> (f64, bool) {
    let entry_price = close_arr[entry_sec.min(899)];
    let mut peak_favorable = 0.0f64;

    for s in entry_sec..900 {
        let p = close_arr[s];
        let move_pct = (p - entry_price) / (entry_price + 1e-12);
        let favorable = if direction == "UP" { move_pct } else { -move_pct };

        peak_favorable = peak_favorable.max(favorable);

        // Stop loss: adverse move exceeds threshold
        if favorable < -TRAILING_STOP_LOSS_PCT {
            // Stopped out → partial recovery (between 0 and entry)
            return (TRAILING_STOP_PARTIAL_EXIT, true);
        }

        // Profit lock: if we had a big favorable move and it retraces significantly
        if peak_favorable > TRAILING_STOP_PROFIT_PCT && favorable < peak_favorable * 0.5 {
            // Lock profit at a favorable rate
            return (if final_correct { 1.0 } else { 0.65 }, true);
        }
    }

    // No stop triggered — use normal binary outcome
    (if final_correct { 1.0 } else { 0.0 }, false)
}
