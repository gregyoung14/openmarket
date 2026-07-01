/// ═══════════════════════════════════════════════════════════════════
/// V8 Smart Value Backtester — Rust Port with v8.1 A/B Testing
/// ═══════════════════════════════════════════════════════════════════
///
/// Runs both v8.0 (original) and v8.1 (enhanced) on the same data
/// for direct comparison.
///
/// v8.0 = Original Python logic, faithfully ported:
///   - 4-component signal: Drift 0.45, Scoreboard 0.25, OFI 0.20, EMA 0.10
///   - Fixed 45s confirmation
///   - First-signal entry
///   - No regime/blacklist/volume filters
///
/// v8.1 = Same signal math + 6 proven filters from v10/v11:
///   1. Time-of-day / day-of-week blacklist
///   2. Regime filter (skip Chop markets)
///   3. Best-signal mode (peak confidence entry)
///   4. Settle price fix (first trade AFTER window)
///   5. Volume gate (skip low-volume windows)
///   6. Adaptive confirmation (volatility-scaled)

use anyhow::Result;
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use rayon::prelude::*;
use rusqlite::Connection;
use serde::Serialize;
use statrs::distribution::{ContinuousCDF, Normal};
use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Instant;
use prettytable::{Table, row};

// ─────────────────────────────────────────────────────────────────
// CLI
// ─────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(author, version, about = "V8 / V8.1 A/B Backtester (Rust)")]
struct Args {
    #[arg(long, default_value = "data/polymarket_btc_data.db")]
    db_path: PathBuf,

    #[arg(long, default_value_t = 100.0)]
    bankroll: f64,

    #[arg(long, default_value_t = 0.05)]
    bet_fraction: f64,
}

// ═════════════════════════════════════════════════════════════════
// CONSTANTS
// ═════════════════════════════════════════════════════════════════

// ── Shared (v8.0 and v8.1) ──
const SLIPPAGE: f64 = 0.005;
const FEE_RATE: f64 = 0.01;

const W_DRIFT: f64 = 0.45;
const W_SCOREBOARD: f64 = 0.25;
const W_OFI: f64 = 0.20;
const W_EMA: f64 = 0.10;

const MIN_SECS_INTO_MARKET: i64 = 60;
const MAX_SECS_INTO_MARKET: i64 = 600;
const MARKET_DURATION_SECS: i64 = 900;

const MIN_ENTRY_PRICE: f64 = 0.15;
const MAX_ENTRY_PRICE: f64 = 0.75;
const MIN_EDGE: f64 = 0.05;
const MAX_DAILY_LOSS_PCT: f64 = 0.20;

const MOMENTUM_TP: f64 = 0.10;

const CONFIDENCE_LEVELS: [f64; 5] = [0.65, 0.70, 0.75, 0.80, 0.85];
const MOMENTUM_CONF_LEVELS: [f64; 3] = [0.55, 0.60, 0.65];

// ── v8.0 only ──
const V80_CONFIRMATION_WINDOW: i64 = 45;

// ── v8.1 enhancements ──

// Regime detection (from v10/v11)
const REGIME_TREND_THRESHOLD: f64 = 0.15;
const REGIME_CHOP_THRESHOLD: f64 = 0.06;
const REGIME_AUTOCORR_CHOP: f64 = -0.25;
const REGIME_LOOKBACK: usize = 60;
const NEUTRAL_CONF_PENALTY: f64 = 0.02;

// Adaptive confirmation (from v10/v11)
const BASE_CONFIRM_WINDOW: i64 = 30;
const MIN_CONFIRM_WINDOW: i64 = 15;
const MAX_CONFIRM_WINDOW: i64 = 50;

// ═════════════════════════════════════════════════════════════════
// DATA STRUCTURES
// ═════════════════════════════════════════════════════════════════

#[derive(Debug, Clone)]
struct BinanceTrade {
    trade_time: i64,
    price: f64,
    quantity: f64,
    is_buyer_maker: i32,
}

#[derive(Debug, Clone)]
struct PolyTick {
    market_slug: String,
    source_ts_ms: i64,
    side_label: String,
    best_bid: f64,
    best_ask: f64,
}

#[derive(Debug, Clone)]
struct MarketMeta {
    market_slug: String,
}

// ── Signal output for v8.0 (first-signal mode) ──
#[derive(Debug, Clone)]
struct MarketSignalV80 {
    slug: String,
    start_ms: i64,
    end_ms: i64,
    actual: String,
    signal: String,
    confidence: f64,
    consistency: f64,
    entry_up_ask: f64,
    entry_down_ask: f64,
    entry_secs_in: i64,
    up_trajectory: Vec<(i64, f64, f64)>,
    down_trajectory: Vec<(i64, f64, f64)>,
}

// ── Signal output for v8.1 (best-signal mode) ──
#[derive(Debug, Clone)]
struct MarketSignalV81 {
    slug: String,
    start_ms: i64,
    end_ms: i64,
    actual: String,
    signal: String,
    confidence: f64,
    consistency: f64,
    entry_up_ask: f64,
    entry_down_ask: f64,
    entry_secs_in: i64,
    regime: String,
    up_trajectory: Vec<(i64, f64, f64)>,
    down_trajectory: Vec<(i64, f64, f64)>,
}

#[derive(Debug, Serialize, Clone)]
struct TradeLog {
    market: String,
    entry_secs_in: i64,
    side: String,
    entry_price: f64,
    exit_price: f64,
    bet_amount: f64,
    pnl: f64,
    bankroll: f64,
    confidence: f64,
    edge: f64,
    actual: String,
    correct: bool,
    strategy: String,
    exit_type: String,
}

// ═════════════════════════════════════════════════════════════════
// DATA LOADING
// ═════════════════════════════════════════════════════════════════

fn load_data(db_path: &PathBuf) -> Result<(Vec<MarketMeta>, Vec<BinanceTrade>, Vec<PolyTick>)> {
    println!("  Connecting to database: {:?}", db_path);
    let conn = Connection::open(db_path)?;

    println!("  Loading market_meta...");
    let mut stmt = conn.prepare(
        "SELECT market_slug FROM market_meta ORDER BY first_seen_ms ASC",
    )?;
    let meta: Vec<MarketMeta> = stmt
        .query_map([], |row| {
            Ok(MarketMeta {
                market_slug: row.get(0)?,
            })
        })?
        .filter_map(|r| r.ok())
        .collect();

    println!("  Loading binance_trades...");
    let mut stmt = conn.prepare(
        "SELECT trade_time, price, quantity, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC",
    )?;
    let trades: Vec<BinanceTrade> = stmt
        .query_map([], |row| {
            Ok(BinanceTrade {
                trade_time: row.get(0)?,
                price: row.get(1)?,
                quantity: row.get(2)?,
                is_buyer_maker: row.get(3)?,
            })
        })?
        .filter_map(|r| r.ok())
        .collect();

    println!("  Loading polymarket_ticks_ms...");
    let mut stmt = conn.prepare(
        "SELECT market_slug, source_ts_ms, side_label, best_bid, best_ask \
         FROM polymarket_ticks_ms \
         WHERE event_type = 'price_change' \
         ORDER BY source_ts_ms ASC",
    )?;
    let ticks: Vec<PolyTick> = stmt
        .query_map([], |row| {
            Ok(PolyTick {
                market_slug: row.get(0)?,
                source_ts_ms: row.get(1)?,
                side_label: row.get(2)?,
                best_bid: row.get(3)?,
                best_ask: row.get(4)?,
            })
        })?
        .filter_map(|r| r.ok())
        .collect();

    println!(
        "  Loaded: {} markets, {} trades, {} ticks",
        meta.len(), trades.len(), ticks.len()
    );
    Ok((meta, trades, ticks))
}

// ═════════════════════════════════════════════════════════════════
// BLACKLIST (from v11)
// ═════════════════════════════════════════════════════════════════

fn build_blacklist() -> HashSet<(u32, u32)> {
    let mut s = HashSet::new();

    // Global bad hours (all days)
    for dow in 0..7u32 {
        for &h in &[0u32, 9, 10, 15, 16] {
            s.insert((dow, h));
        }
    }

    // Monday
    s.insert((0, 13));
    s.insert((0, 18));
    s.insert((0, 20));
    // Tuesday
    s.insert((1, 3));
    s.insert((1, 5));
    s.insert((1, 6));
    s.insert((1, 7));
    s.insert((1, 8));
    s.insert((1, 18));
    s.insert((1, 21));
    s.insert((1, 23));
    // Wednesday
    s.insert((2, 7));
    s.insert((2, 13));
    s.insert((2, 18));
    s.insert((2, 22));
    // Thursday
    s.insert((3, 6));
    s.insert((3, 19));
    s.insert((3, 23));
    // Friday
    s.insert((4, 7));
    s.insert((4, 12));
    s.insert((4, 13));
    s.insert((4, 14));
    s.insert((4, 17));
    s.insert((4, 18));
    s.insert((4, 19));
    s.insert((4, 23));
    // Saturday
    s.insert((5, 3));
    s.insert((5, 5));
    s.insert((5, 6));
    s.insert((5, 21));
    s.insert((5, 23));
    // Sunday
    s.insert((6, 1));
    s.insert((6, 3));
    s.insert((6, 20));
    s.insert((6, 22));
    s.insert((6, 23));

    s
}

fn is_blacklisted(epoch_s: i64, blacklist: &HashSet<(u32, u32)>) -> bool {
    let et_hour = ((epoch_s / 3600 % 24) - 5).rem_euclid(24) as u32;
    let et_epoch = epoch_s - 5 * 3600;
    let days_since_epoch = et_epoch / 86400;
    let dow = ((days_since_epoch + 3) % 7) as u32;
    blacklist.contains(&(dow, et_hour))
}

// ═════════════════════════════════════════════════════════════════
// REGIME DETECTION (from v10/v11)
// ═════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, PartialEq)]
enum Regime {
    Trend,
    Chop,
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

fn detect_regime(closes: &[f64]) -> (Regime, f64, f64) {
    let n = closes.len();
    if n < 15 {
        return (Regime::Neutral, 0.0, 0.0);
    }

    let start_idx = if n > REGIME_LOOKBACK { n - REGIME_LOOKBACK } else { 0 };
    let valid: Vec<f64> = closes[start_idx..]
        .iter()
        .copied()
        .filter(|&x| x > 0.0)
        .collect();

    if valid.len() < 15 {
        return (Regime::Neutral, 0.0, 0.0);
    }

    let direct = (valid[valid.len() - 1] - valid[0]).abs();
    let total_path: f64 = valid.windows(2).map(|w| (w[1] - w[0]).abs()).sum();
    let path_eff = direct / (total_path + 1e-12);

    let returns: Vec<f64> = (1..valid.len())
        .map(|i| (valid[i] / (valid[i - 1] + 1e-9)).ln())
        .collect();

    let autocorr = compute_autocorrelation(&returns);

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

fn compute_autocorrelation(returns: &[f64]) -> f64 {
    if returns.len() <= 5 {
        return 0.0;
    }
    let x = &returns[..returns.len() - 1];
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
}

// ═════════════════════════════════════════════════════════════════
// 4-COMPONENT DRIFT SIGNAL (identical for v8.0 and v8.1)
// ═════════════════════════════════════════════════════════════════

struct DriftResult {
    direction: String,
    confidence: f64,
    consistency: f64,
}

fn compute_drift_signal(
    prices: &[f64],
    is_buyer_maker: &[i32],
    quantities: &[f64],
    open_price: f64,
    entry_seconds: i64,
    remaining_seconds: i64,
) -> Option<DriftResult> {
    let n = prices.len();
    if n < 10 {
        return None;
    }
    let current_price = prices[n - 1];

    // Component 1: Brownian Drift
    let mut log_returns = Vec::with_capacity(n - 1);
    for i in 1..n {
        log_returns.push((prices[i] + 1e-9).ln() - (prices[i - 1] + 1e-9).ln());
    }
    if log_returns.len() < 5 {
        return None;
    }
    let dt = entry_seconds as f64 / log_returns.len() as f64;
    let mu: f64 = log_returns.iter().sum::<f64>() / log_returns.len() as f64 / (dt + 1e-9);
    let mean_lr = log_returns.iter().sum::<f64>() / log_returns.len() as f64;
    let sigma = (log_returns.iter().map(|r| (r - mean_lr).powi(2)).sum::<f64>()
        / log_returns.len() as f64)
        .sqrt()
        / (dt.sqrt() + 1e-9);

    let drift_prob_up = if sigma > 0.0 && remaining_seconds > 0 {
        let z = mu * (remaining_seconds as f64).sqrt() / sigma;
        Normal::new(0.0, 1.0).unwrap().cdf(z)
    } else {
        0.5
    };

    // Component 2: Scoreboard
    let price_vs_open = (current_price - open_price) / (open_price + 1e-9);
    let scoreboard_signal = 1.0 / (1.0 + (-price_vs_open * 5000.0).exp());

    // Component 3: OFI
    let (mut buy_vol, mut sell_vol) = (0.0_f64, 0.0_f64);
    for i in 0..n {
        if is_buyer_maker[i] == 0 {
            buy_vol += quantities[i];
        } else {
            sell_vol += quantities[i];
        }
    }
    let ofi = (buy_vol - sell_vol) / (buy_vol + sell_vol + 1e-9);
    let ofi_signal = 1.0 / (1.0 + (-ofi * 3.0).exp());

    // Component 4: EMA cross
    let ema_fast_span = 10.min(n / 2 + 1).max(1);
    let ema_slow_span = 60.min(n).max(1);
    let ema_fast = ema(prices, ema_fast_span);
    let ema_slow = ema(prices, ema_slow_span);
    let ema_cross = (ema_fast - ema_slow) / (ema_slow + 1e-9);
    let ema_signal = 1.0 / (1.0 + (-ema_cross * 5000.0).exp());

    // Weighted combination (v8 weights)
    let combined = W_DRIFT * drift_prob_up
        + W_SCOREBOARD * scoreboard_signal
        + W_OFI * ofi_signal
        + W_EMA * ema_signal;

    let (direction, confidence) = if combined > 0.5 {
        ("UP".to_string(), combined)
    } else {
        ("DOWN".to_string(), 1.0 - combined)
    };

    let signals_up = [
        drift_prob_up > 0.5,
        scoreboard_signal > 0.5,
        ofi_signal > 0.5,
        ema_signal > 0.5,
    ];
    let consistency = if direction == "UP" {
        signals_up.iter().filter(|&&s| s).count() as f64 / 4.0
    } else {
        signals_up.iter().filter(|&&s| !s).count() as f64 / 4.0
    };

    Some(DriftResult { direction, confidence, consistency })
}

fn ema(prices: &[f64], span: usize) -> f64 {
    if prices.is_empty() { return 0.0; }
    let alpha = 2.0 / (span as f64 + 1.0);
    let mut val = prices[0];
    for &p in &prices[1..] {
        val = alpha * p + (1.0 - alpha) * val;
    }
    val
}

// ═════════════════════════════════════════════════════════════════
// V8.1: ADAPTIVE CONFIRMATION (from v10/v11)
// ═════════════════════════════════════════════════════════════════

fn compute_adaptive_confirm(prices: &[f64]) -> i64 {
    let n = prices.len();
    if n < 10 {
        return BASE_CONFIRM_WINDOW;
    }

    let log_returns: Vec<f64> = (1..n)
        .map(|i| (prices[i] / (prices[i - 1] + 1e-9)).ln())
        .collect();

    let recent_rets = if log_returns.len() > 30 {
        &log_returns[log_returns.len() - 30..]
    } else {
        &log_returns
    };

    let vol = if recent_rets.len() > 3 {
        let m = recent_rets.iter().sum::<f64>() / recent_rets.len() as f64;
        (recent_rets.iter().map(|r| (r - m).powi(2)).sum::<f64>()
            / recent_rets.len() as f64)
            .sqrt()
    } else {
        0.0
    };

    let vol_score = (vol / 0.0002).min(2.0);
    let adaptive = (BASE_CONFIRM_WINDOW as f64 * (1.3 - 0.3 * vol_score).max(0.5)) as i64;
    adaptive.clamp(MIN_CONFIRM_WINDOW, MAX_CONFIRM_WINDOW)
}

// ═════════════════════════════════════════════════════════════════
// V8.1: VOLUME GATE
// ═════════════════════════════════════════════════════════════════

fn compute_median_volume(trades: &[BinanceTrade], meta: &[MarketMeta]) -> f64 {
    let mut window_vols: Vec<f64> = Vec::new();

    for market in meta {
        let epoch_s: i64 = market
            .market_slug
            .split('-')
            .last()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let start_ms = epoch_s * 1000;
        let end_ms = start_ms + MARKET_DURATION_SECS * 1000;

        let lo = trades.partition_point(|t| t.trade_time < start_ms);
        let hi = trades.partition_point(|t| t.trade_time < end_ms);

        let vol: f64 = trades[lo..hi].iter().map(|t| t.quantity).sum();
        if vol > 0.0 {
            window_vols.push(vol);
        }
    }

    if window_vols.is_empty() {
        return 0.0;
    }
    window_vols.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mid = window_vols.len() / 2;
    window_vols[mid]
}

// ═════════════════════════════════════════════════════════════════
// POLYMARKET TICK HELPERS
// ═════════════════════════════════════════════════════════════════

fn find_entry_ask(ticks: &[PolyTick], slug: &str, direction: &str, current_ms: i64) -> f64 {
    for t in ticks.iter() {
        if t.market_slug == slug
            && t.side_label == direction
            && t.source_ts_ms >= current_ms
            && t.source_ts_ms < current_ms + 10_000
        {
            return t.best_ask;
        }
    }
    0.50
}

fn build_trajectories(
    ticks: &[PolyTick],
    slug: &str,
    current_ms: i64,
) -> (Vec<(i64, f64, f64)>, Vec<(i64, f64, f64)>) {
    let mut up_traj = Vec::new();
    let mut down_traj = Vec::new();
    for t in ticks.iter() {
        if t.market_slug == slug && t.source_ts_ms >= current_ms {
            let entry = (t.source_ts_ms, t.best_bid, t.best_ask);
            if t.side_label == "UP" {
                up_traj.push(entry);
            } else if t.side_label == "DOWN" {
                down_traj.push(entry);
            }
        }
    }
    (up_traj, down_traj)
}

// ═════════════════════════════════════════════════════════════════
// v8.0 SIGNAL BUILDER (original: first-signal, no filters)
// ═════════════════════════════════════════════════════════════════

fn build_signals_v80(
    meta: &[MarketMeta],
    trades: &[BinanceTrade],
    ticks: &[PolyTick],
    pb: &ProgressBar,
) -> Vec<MarketSignalV80> {
    let results: Vec<Option<MarketSignalV80>> = meta
        .par_iter()
        .map(|market| {
            pb.inc(1);
            let slug = &market.market_slug;
            let epoch_s: i64 = slug.split('-').last().and_then(|s| s.parse().ok()).unwrap_or(0);
            let start_ms = epoch_s * 1000;
            let end_ms = start_ms + MARKET_DURATION_SECS * 1000;

            let lo = trades.partition_point(|t| t.trade_time < start_ms);
            let hi = trades.partition_point(|t| t.trade_time < end_ms);
            let market_trades = &trades[lo..hi];
            if market_trades.len() < 50 { return None; }

            let btc_start = market_trades[0].price;
            // v8.0: uses last trade IN window (original behavior)
            let btc_end = market_trades[market_trades.len() - 1].price;
            let actual = if btc_end > btc_start { "UP" } else { "DOWN" };

            let prices: Vec<f64> = market_trades.iter().map(|t| t.price).collect();
            let quantities: Vec<f64> = market_trades.iter().map(|t| t.quantity).collect();
            let makers: Vec<i32> = market_trades.iter().map(|t| t.is_buyer_maker).collect();
            let times: Vec<i64> = market_trades.iter().map(|t| t.trade_time).collect();

            let mut confirm_count: i64 = 0;
            let mut confirm_direction: Option<String> = None;

            for s in MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET {
                let current_ms = start_ms + s * 1000;
                let remaining_s = MARKET_DURATION_SECS - s;
                let end_idx = times.partition_point(|&t| t < current_ms);
                if end_idx < 20 { continue; }

                let result = match compute_drift_signal(
                    &prices[..end_idx], &makers[..end_idx], &quantities[..end_idx],
                    btc_start, s, remaining_s,
                ) {
                    Some(r) => r,
                    None => { confirm_count = 0; confirm_direction = None; continue; }
                };

                if result.confidence >= CONFIDENCE_LEVELS[0] {
                    if confirm_direction.as_deref() == Some(&result.direction) {
                        confirm_count += 1;
                    } else {
                        confirm_direction = Some(result.direction.clone());
                        confirm_count = 1;
                    }

                    if confirm_count >= V80_CONFIRMATION_WINDOW {
                        let dir = confirm_direction.as_ref().unwrap();
                        let entry_ask = find_entry_ask(ticks, slug, dir, current_ms);
                        let (up_traj, down_traj) = build_trajectories(ticks, slug, current_ms);

                        return Some(MarketSignalV80 {
                            slug: slug.clone(),
                            start_ms: current_ms,
                            end_ms,
                            actual: actual.to_string(),
                            signal: dir.clone(),
                            confidence: result.confidence,
                            consistency: result.consistency,
                            entry_up_ask: if dir == "UP" { entry_ask } else { 0.50 },
                            entry_down_ask: if dir == "DOWN" { entry_ask } else { 0.50 },
                            entry_secs_in: s,
                            up_trajectory: up_traj,
                            down_trajectory: down_traj,
                        });
                    }
                } else {
                    confirm_count = 0;
                    confirm_direction = None;
                }
            }
            None
        })
        .collect();

    results.into_iter().flatten().collect()
}

// ═════════════════════════════════════════════════════════════════
// v8.1 SIGNAL BUILDER (enhanced: all 6 filters)
// ═════════════════════════════════════════════════════════════════

fn build_signals_v81(
    meta: &[MarketMeta],
    trades: &[BinanceTrade],
    ticks: &[PolyTick],
    blacklist: &HashSet<(u32, u32)>,
    median_vol: f64,
    pb: &ProgressBar,
) -> Vec<MarketSignalV81> {
    let results: Vec<Option<MarketSignalV81>> = meta
        .par_iter()
        .map(|market| {
            pb.inc(1);
            let slug = &market.market_slug;
            let epoch_s: i64 = slug.split('-').last().and_then(|s| s.parse().ok()).unwrap_or(0);
            let start_ms = epoch_s * 1000;
            let end_ms = start_ms + MARKET_DURATION_SECS * 1000;

            // ── FILTER 1: Blacklist ──
            if is_blacklisted(epoch_s, blacklist) {
                return None;
            }

            let lo = trades.partition_point(|t| t.trade_time < start_ms);
            let hi = trades.partition_point(|t| t.trade_time < end_ms);
            let market_trades = &trades[lo..hi];
            if market_trades.len() < 50 { return None; }

            // ── FILTER 5: Volume gate ──
            let window_vol: f64 = market_trades.iter().map(|t| t.quantity).sum();
            if window_vol < median_vol {
                return None;
            }

            let btc_start = market_trades[0].price;

            // ── FIX 4: Settle price = first trade AFTER window ──
            let settle_idx = trades.partition_point(|t| t.trade_time < end_ms);
            let btc_end = if settle_idx < trades.len() {
                trades[settle_idx].price
            } else {
                market_trades[market_trades.len() - 1].price
            };
            let actual = if btc_end > btc_start { "UP" } else { "DOWN" };

            // Build 1s bars for regime detection + adaptive confirm
            let mut close_arr = vec![0.0_f64; 900];
            for t in market_trades {
                let sec = ((t.trade_time - start_ms) / 1000).clamp(0, 899) as usize;
                close_arr[sec] = t.price;
            }
            // Forward-fill
            let mut cur = btc_start;
            for c in close_arr.iter_mut() {
                if *c == 0.0 { *c = cur; } else { cur = *c; }
            }

            let prices: Vec<f64> = market_trades.iter().map(|t| t.price).collect();
            let quantities: Vec<f64> = market_trades.iter().map(|t| t.quantity).collect();
            let makers: Vec<i32> = market_trades.iter().map(|t| t.is_buyer_maker).collect();
            let times: Vec<i64> = market_trades.iter().map(|t| t.trade_time).collect();

            // ── ENHANCEMENT 3: Best-signal mode ──
            // Instead of first confirmed signal, track the BEST one
            let mut best_signal: Option<(i64, String, f64, f64, String)> = None; // (sec, dir, conf, consistency, regime)
            let mut confirm_count: i64 = 0;
            let mut confirm_direction: Option<String> = None;

            for s in MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET {
                let current_ms_iter = start_ms + s * 1000;
                let remaining_s = MARKET_DURATION_SECS - s;
                let end_idx = times.partition_point(|&t| t < current_ms_iter);
                if end_idx < 20 { continue; }

                let result = match compute_drift_signal(
                    &prices[..end_idx], &makers[..end_idx], &quantities[..end_idx],
                    btc_start, s, remaining_s,
                ) {
                    Some(r) => r,
                    None => { confirm_count = 0; confirm_direction = None; continue; }
                };

                // ── FILTER 2: Regime detection ──
                let idx = (s as usize).min(899);
                let (regime, _, _) = detect_regime(&close_arr[..=idx]);
                if regime == Regime::Chop {
                    confirm_count = 0;
                    confirm_direction = None;
                    continue;
                }

                // Apply neutral penalty (from v10/v11)
                let mut adjusted_conf = result.confidence;
                if regime == Regime::Neutral {
                    adjusted_conf -= NEUTRAL_CONF_PENALTY;
                }

                if adjusted_conf >= CONFIDENCE_LEVELS[0] {
                    if confirm_direction.as_deref() == Some(&result.direction) {
                        confirm_count += 1;
                    } else {
                        confirm_direction = Some(result.direction.clone());
                        confirm_count = 1;
                    }

                    // ── ENHANCEMENT 6: Adaptive confirmation ──
                    let adaptive_window = compute_adaptive_confirm(&prices[..end_idx]);

                    if confirm_count >= adaptive_window {
                        // It's a candidate — is it the BEST we've seen?
                        let is_better = match &best_signal {
                            Some((_, _, prev_conf, _, _)) => adjusted_conf > *prev_conf,
                            None => true,
                        };
                        if is_better {
                            best_signal = Some((
                                s,
                                confirm_direction.as_ref().unwrap().clone(),
                                adjusted_conf,
                                result.consistency,
                                format!("{}", regime),
                            ));
                        }
                    }
                } else {
                    confirm_count = 0;
                    confirm_direction = None;
                }
            }

            // Convert best signal to MarketSignalV81
            let (best_sec, best_dir, best_conf, best_consistency, best_regime) = best_signal?;
            let best_ms = start_ms + best_sec * 1000;
            let entry_ask = find_entry_ask(ticks, slug, &best_dir, best_ms);
            let (up_traj, down_traj) = build_trajectories(ticks, slug, best_ms);

            Some(MarketSignalV81 {
                slug: slug.clone(),
                start_ms: best_ms,
                end_ms,
                actual: actual.to_string(),
                signal: best_dir.clone(),
                confidence: best_conf,
                consistency: best_consistency,
                entry_up_ask: if best_dir == "UP" { entry_ask } else { 0.50 },
                entry_down_ask: if best_dir == "DOWN" { entry_ask } else { 0.50 },
                entry_secs_in: best_sec,
                regime: best_regime,
                up_trajectory: up_traj,
                down_trajectory: down_traj,
            })
        })
        .collect();

    results.into_iter().flatten().collect()
}

// ═════════════════════════════════════════════════════════════════
// BACKTEST ENGINES (shared by both versions)
// ═════════════════════════════════════════════════════════════════

trait Signal {
    fn slug(&self) -> &str;
    fn actual(&self) -> &str;
    fn signal(&self) -> &str;
    fn confidence(&self) -> f64;
    fn entry_up_ask(&self) -> f64;
    fn entry_down_ask(&self) -> f64;
    fn entry_secs_in(&self) -> i64;
    fn up_trajectory(&self) -> &[(i64, f64, f64)];
    fn down_trajectory(&self) -> &[(i64, f64, f64)];
}

impl Signal for MarketSignalV80 {
    fn slug(&self) -> &str { &self.slug }
    fn actual(&self) -> &str { &self.actual }
    fn signal(&self) -> &str { &self.signal }
    fn confidence(&self) -> f64 { self.confidence }
    fn entry_up_ask(&self) -> f64 { self.entry_up_ask }
    fn entry_down_ask(&self) -> f64 { self.entry_down_ask }
    fn entry_secs_in(&self) -> i64 { self.entry_secs_in }
    fn up_trajectory(&self) -> &[(i64, f64, f64)] { &self.up_trajectory }
    fn down_trajectory(&self) -> &[(i64, f64, f64)] { &self.down_trajectory }
}

impl Signal for MarketSignalV81 {
    fn slug(&self) -> &str { &self.slug }
    fn actual(&self) -> &str { &self.actual }
    fn signal(&self) -> &str { &self.signal }
    fn confidence(&self) -> f64 { self.confidence }
    fn entry_up_ask(&self) -> f64 { self.entry_up_ask }
    fn entry_down_ask(&self) -> f64 { self.entry_down_ask }
    fn entry_secs_in(&self) -> i64 { self.entry_secs_in }
    fn up_trajectory(&self) -> &[(i64, f64, f64)] { &self.up_trajectory }
    fn down_trajectory(&self) -> &[(i64, f64, f64)] { &self.down_trajectory }
}

fn backtest_hold_to_resolve<S: Signal>(
    signals: &[S],
    initial_bankroll: f64,
    bet_frac: f64,
    min_conf: f64,
) -> (Vec<TradeLog>, f64) {
    let mut bankroll = initial_bankroll;
    let mut peak = initial_bankroll;
    let mut halted = false;
    let mut log = Vec::new();

    for s in signals {
        if !halted {
            let dd = (peak - bankroll) / peak;
            if dd >= MAX_DAILY_LOSS_PCT { halted = true; }
        }
        if halted { continue; }
        if s.confidence() < min_conf { continue; }

        let entry_ask = if s.signal() == "UP" { s.entry_up_ask() } else { s.entry_down_ask() };
        if entry_ask <= 0.0 || entry_ask >= 1.0 { continue; }
        if entry_ask < MIN_ENTRY_PRICE || entry_ask > MAX_ENTRY_PRICE { continue; }

        let entry_price = entry_ask + SLIPPAGE;
        let edge = s.confidence() - entry_price;
        if edge < MIN_EDGE { continue; }

        let bet_amount = bankroll * bet_frac;
        let fee_entry = bet_amount * FEE_RATE;
        let shares = (bet_amount - fee_entry) / entry_price;

        let correct = s.signal() == s.actual();
        let payout = if correct { shares } else { 0.0 };
        let fee_exit = payout * FEE_RATE;
        let pnl = payout - fee_exit - bet_amount;

        bankroll += pnl;
        peak = peak.max(bankroll);

        log.push(TradeLog {
            market: s.slug().to_string(),
            entry_secs_in: s.entry_secs_in(),
            side: s.signal().to_string(),
            entry_price: round4(entry_price),
            exit_price: if correct { 1.0 } else { 0.0 },
            bet_amount: round2(bet_amount),
            pnl: round2(pnl),
            bankroll: round2(bankroll),
            confidence: round4(s.confidence()),
            edge: round4(edge),
            actual: s.actual().to_string(),
            correct,
            strategy: "HOLD_TO_RESOLVE".to_string(),
            exit_type: if correct { "RESOLVE_WIN".to_string() } else { "RESOLVE_LOSS".to_string() },
        });
    }
    (log, bankroll)
}

fn backtest_momentum<S: Signal>(
    signals: &[S],
    initial_bankroll: f64,
    bet_frac: f64,
    min_conf: f64,
    take_profit: f64,
) -> (Vec<TradeLog>, f64) {
    let mut bankroll = initial_bankroll;
    let mut peak = initial_bankroll;
    let mut halted = false;
    let mut log = Vec::new();

    for s in signals {
        if !halted {
            let dd = (peak - bankroll) / peak;
            if dd >= MAX_DAILY_LOSS_PCT { halted = true; }
        }
        if halted { continue; }
        if s.confidence() < min_conf { continue; }

        let entry_ask = if s.signal() == "UP" { s.entry_up_ask() } else { s.entry_down_ask() };
        let trajectory = if s.signal() == "UP" { s.up_trajectory() } else { s.down_trajectory() };
        if entry_ask <= 0.0 || entry_ask >= 1.0 { continue; }

        let entry_price = (entry_ask + SLIPPAGE).min(MAX_ENTRY_PRICE);
        let bet_amount = bankroll * bet_frac;
        let fee_entry = bet_amount * FEE_RATE;
        let shares = (bet_amount - fee_entry) / entry_price;

        let tp_price = entry_price + take_profit;
        let mut exit_price: Option<f64> = None;
        let mut exit_type = "RESOLVE".to_string();

        if tp_price < MAX_ENTRY_PRICE {
            for &(_, bid, _) in trajectory.iter() {
                if bid >= tp_price {
                    exit_price = Some(bid - SLIPPAGE);
                    exit_type = "TAKE_PROFIT".to_string();
                    break;
                }
            }
        }

        let correct = s.signal() == s.actual();
        let final_exit = exit_price.unwrap_or(if correct { 1.0 } else { 0.0 });
        if exit_price.is_none() {
            exit_type = if correct { "RESOLVE_WIN".to_string() } else { "RESOLVE_LOSS".to_string() };
        }

        let payout = shares * final_exit;
        let fee_exit_val = if payout > 0.0 { payout * FEE_RATE } else { 0.0 };
        let pnl = payout - fee_exit_val - bet_amount;

        bankroll += pnl;
        peak = peak.max(bankroll);

        log.push(TradeLog {
            market: s.slug().to_string(),
            entry_secs_in: s.entry_secs_in(),
            side: s.signal().to_string(),
            entry_price: round4(entry_price),
            exit_price: round4(final_exit),
            bet_amount: round2(bet_amount),
            pnl: round2(pnl),
            bankroll: round2(bankroll),
            confidence: round4(s.confidence()),
            edge: round4(s.confidence() - entry_price),
            actual: s.actual().to_string(),
            correct,
            strategy: format!("MOMENTUM_TP{}", (take_profit * 100.0) as i32),
            exit_type,
        });
    }
    (log, bankroll)
}

// ═════════════════════════════════════════════════════════════════
// REPORTING
// ═════════════════════════════════════════════════════════════════

fn compute_stats(log: &[TradeLog], initial_bankroll: f64) -> (usize, f64, f64, f64, f64) {
    let trades = log.len();
    if trades == 0 { return (0, 0.0, 0.0, 0.0, 0.0); }
    let wins = log.iter().filter(|t| t.pnl > 0.0).count();
    let wr = wins as f64 / trades as f64 * 100.0;
    let total_pnl: f64 = log.iter().map(|t| t.pnl).sum();
    let final_b = initial_bankroll + total_pnl;
    let roi = (final_b - initial_bankroll) / initial_bankroll * 100.0;
    let mut pk = initial_bankroll;
    let mut mdd = 0.0_f64;
    let mut eq = initial_bankroll;
    for t in log {
        eq += t.pnl;
        pk = pk.max(eq);
        mdd = mdd.max((pk - eq) / pk);
    }
    (wins, wr, roi, mdd, total_pnl)
}

fn round2(v: f64) -> f64 { (v * 100.0).round() / 100.0 }
fn round4(v: f64) -> f64 { (v * 10000.0).round() / 10000.0 }

struct StrategyResult {
    name: String,
    log: Vec<TradeLog>,
    final_bankroll: f64,
}

fn run_sweep<S: Signal>(
    label: &str,
    signals: &[S],
    bankroll: f64,
    bet_fraction: f64,
) -> Vec<StrategyResult> {
    let mut results = Vec::new();

    // Hold-to-resolve sweep
    for &conf in CONFIDENCE_LEVELS.iter() {
        let name = format!("{} Hold (>{:.0}%)", label, conf * 100.0);
        let (log, final_b) = backtest_hold_to_resolve(signals, bankroll, bet_fraction, conf);
        results.push(StrategyResult { name, log, final_bankroll: final_b });
    }

    // Momentum sweep
    for &conf in MOMENTUM_CONF_LEVELS.iter() {
        let name = format!("{} Mom TP10% >{:.0}%", label, conf * 100.0);
        let (log, final_b) = backtest_momentum(signals, bankroll, bet_fraction, conf, MOMENTUM_TP);
        results.push(StrategyResult { name, log, final_bankroll: final_b });
    }

    results
}

// ═════════════════════════════════════════════════════════════════
// MAIN
// ═════════════════════════════════════════════════════════════════

fn main() -> Result<()> {
    let t0 = Instant::now();
    let args = Args::parse();

    println!("══════════════════════════════════════════════════════════════");
    println!(" V8.0 vs V8.1 A/B BACKTESTER");
    println!("══════════════════════════════════════════════════════════════");
    println!("  v8.0: Original (first-signal, no filters, 45s confirm)");
    println!("  v8.1: +Blacklist +Regime +BestSignal +SettleFix +VolGate +AdaptiveConfirm");
    println!("  Signal:  Drift 0.45 + Scoreboard 0.25 + OFI 0.20 + EMA 0.10 (IDENTICAL)");
    println!("  Bankroll: ${:.0}  |  Bet: {:.0}%  |  Slip: ${}  |  Fee: {:.0}%",
             args.bankroll, args.bet_fraction * 100.0, SLIPPAGE, FEE_RATE * 100.0);
    println!("  Max Entry Price: ${}  |  Min Edge: {:.0}%  |  Max DD: {:.0}%",
             MAX_ENTRY_PRICE, MIN_EDGE * 100.0, MAX_DAILY_LOSS_PCT * 100.0);

    // Load
    let (meta, trades, ticks) = load_data(&args.db_path)?;

    // Pre-compute v8.1 filters
    let blacklist = build_blacklist();
    println!("  Blacklisted (dow×hour) combos: {}", blacklist.len());

    let median_vol = compute_median_volume(&trades, &meta);
    println!("  Median window volume: {:.2} BTC", median_vol);

    // ──────────────────────────────────────────────────────────────
    // Build signals for both versions
    // ──────────────────────────────────────────────────────────────
    println!("\n  ── Building v8.0 signals (original) ──");
    let pb0 = ProgressBar::new(meta.len() as u64);
    pb0.set_style(ProgressStyle::default_bar()
        .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta})")
        .unwrap().progress_chars("#>-"));
    let signals_v80 = build_signals_v80(&meta, &trades, &ticks, &pb0);
    pb0.finish_with_message("v8.0 done");

    let v80_correct = signals_v80.iter().filter(|s| s.signal == s.actual).count();
    println!("  v8.0 signals: {} | accuracy: {}/{} = {:.1}%",
             signals_v80.len(), v80_correct, signals_v80.len(),
             if signals_v80.is_empty() { 0.0 } else { v80_correct as f64 / signals_v80.len() as f64 * 100.0 });

    println!("\n  ── Building v8.1 signals (enhanced) ──");
    let pb1 = ProgressBar::new(meta.len() as u64);
    pb1.set_style(ProgressStyle::default_bar()
        .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta})")
        .unwrap().progress_chars("#>-"));
    let signals_v81 = build_signals_v81(&meta, &trades, &ticks, &blacklist, median_vol, &pb1);
    pb1.finish_with_message("v8.1 done");

    let v81_correct = signals_v81.iter().filter(|s| s.signal == s.actual).count();
    println!("  v8.1 signals: {} | accuracy: {}/{} = {:.1}%",
             signals_v81.len(), v81_correct, signals_v81.len(),
             if signals_v81.is_empty() { 0.0 } else { v81_correct as f64 / signals_v81.len() as f64 * 100.0 });

    // Count how many markets v8.1 filtered out
    let v81_blacklisted = meta.iter().filter(|m| {
        let epoch_s: i64 = m.market_slug.split('-').last().and_then(|s| s.parse().ok()).unwrap_or(0);
        is_blacklisted(epoch_s, &blacklist)
    }).count();
    println!("\n  v8.1 filter breakdown:");
    println!("    Markets blacklisted: {}", v81_blacklisted);
    println!("    Markets remaining after all filters: {} (→ {} signals)",
             meta.len() - v81_blacklisted, signals_v81.len());

    // ──────────────────────────────────────────────────────────────
    // Run sweeps
    // ──────────────────────────────────────────────────────────────
    let results_v80 = run_sweep("v8.0", &signals_v80, args.bankroll, args.bet_fraction);
    let results_v81 = run_sweep("v8.1", &signals_v81, args.bankroll, args.bet_fraction);

    // ──────────────────────────────────────────────────────────────
    // Side-by-side comparison: Hold-to-Resolve
    // ──────────────────────────────────────────────────────────────
    println!("\n══════════════════════════════════════════════════════════════");
    println!(" A/B COMPARISON: HOLD-TO-RESOLVE");
    println!("══════════════════════════════════════════════════════════════");

    let mut cmp_table = Table::new();
    cmp_table.add_row(row!["Conf", "v8.0 Trades", "v8.0 WR%", "v8.0 ROI",
                            "v8.1 Trades", "v8.1 WR%", "v8.1 ROI", "Δ WR"]);

    for i in 0..CONFIDENCE_LEVELS.len() {
        let (_, wr0, roi0, _, _) = compute_stats(&results_v80[i].log, args.bankroll);
        let (_, wr1, roi1, _, _) = compute_stats(&results_v81[i].log, args.bankroll);
        let delta_wr = wr1 - wr0;

        cmp_table.add_row(row![
            format!("{:.0}%", CONFIDENCE_LEVELS[i] * 100.0),
            results_v80[i].log.len(),
            format!("{:.1}%", wr0),
            format!("{:+.1}%", roi0),
            results_v81[i].log.len(),
            format!("{:.1}%", wr1),
            format!("{:+.1}%", roi1),
            format!("{:+.1}%", delta_wr)
        ]);
    }
    cmp_table.printstd();

    // ──────────────────────────────────────────────────────────────
    // Side-by-side comparison: Momentum
    // ──────────────────────────────────────────────────────────────
    println!("\n══════════════════════════════════════════════════════════════");
    println!(" A/B COMPARISON: MOMENTUM (TP=10%)");
    println!("══════════════════════════════════════════════════════════════");

    let mut mom_table = Table::new();
    mom_table.add_row(row!["Conf", "v8.0 Trades", "v8.0 WR%", "v8.0 ROI",
                            "v8.1 Trades", "v8.1 WR%", "v8.1 ROI", "Δ WR"]);

    let hold_count = CONFIDENCE_LEVELS.len(); // offset for momentum results
    for i in 0..MOMENTUM_CONF_LEVELS.len() {
        let idx0 = hold_count + i;
        let idx1 = hold_count + i;
        let (_, wr0, roi0, _, _) = compute_stats(&results_v80[idx0].log, args.bankroll);
        let (_, wr1, roi1, _, _) = compute_stats(&results_v81[idx1].log, args.bankroll);
        let delta_wr = wr1 - wr0;

        mom_table.add_row(row![
            format!("{:.0}%", MOMENTUM_CONF_LEVELS[i] * 100.0),
            results_v80[idx0].log.len(),
            format!("{:.1}%", wr0),
            format!("{:+.1}%", roi0),
            results_v81[idx1].log.len(),
            format!("{:.1}%", wr1),
            format!("{:+.1}%", roi1),
            format!("{:+.1}%", delta_wr)
        ]);
    }
    mom_table.printstd();

    // ──────────────────────────────────────────────────────────────
    // Overall ranking
    // ──────────────────────────────────────────────────────────────
    println!("\n══════════════════════════════════════════════════════════════");
    println!(" TOP 10 STRATEGIES (all variants ranked)");
    println!("══════════════════════════════════════════════════════════════");

    let mut all: Vec<&StrategyResult> = results_v80.iter().chain(results_v81.iter())
        .filter(|r| !r.log.is_empty())
        .collect();
    all.sort_by(|a, b| b.final_bankroll.partial_cmp(&a.final_bankroll).unwrap());

    let mut rank_table = Table::new();
    rank_table.add_row(row!["#", "Strategy", "Trades", "WR%", "ROI", "MDD", "Final"]);

    for (i, r) in all.iter().enumerate().take(10) {
        let (_, wr, roi, mdd, _) = compute_stats(&r.log, args.bankroll);
        let marker = if i == 0 { " ★" } else { "" };
        rank_table.add_row(row![
            i + 1,
            format!("{}{}", r.name, marker),
            r.log.len(),
            format!("{:.1}%", wr),
            format!("{:+.1}%", roi),
            format!("{:.1}%", mdd * 100.0),
            format!("${:.2}", r.final_bankroll)
        ]);
    }
    rank_table.printstd();

    // ──────────────────────────────────────────────────────────────
    // Save CSV
    // ──────────────────────────────────────────────────────────────
    if let Some(best) = all.first() {
        if !best.log.is_empty() {
            let csv_path = "v8_ab_trade_log.csv";
            let mut wtr = csv::Writer::from_path(csv_path)?;
            for t in &best.log {
                wtr.serialize(t)?;
            }
            wtr.flush()?;
            println!("\n  Best trade log saved to {} ({})", csv_path, best.name);
        }
    }

    let elapsed = t0.elapsed();
    println!("\n  Done in {:.1}s", elapsed.as_secs_f64());

    Ok(())
}
