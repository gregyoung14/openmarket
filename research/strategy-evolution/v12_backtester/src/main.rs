/// ═══════════════════════════════════════════════════════════════
/// V12 Backtester — Binance-Only Directional Accuracy A/B Test
/// ═══════════════════════════════════════════════════════════════
///
/// Tests 7 signal variants on 60 days of Binance data (no Polymarket
/// order book needed). Directional accuracy is the primary metric.
/// Simulated P&L uses an assumed fair-value entry of $0.50.
///
/// Signal Variants:
///   0. baseline_v12    — v10 weights (0.55/0.30/0.15), clean, no whipsaw
///   1. recency_drift   — Exponential decay on log returns (lambda=0.05)
///   2. multiwindow_ofi — 3-window OFI: last 30s / 90s / all
///   3. momentum_accel  — 4th component: 30s vs 90s price return delta
///   4. regime_weights  — Neutral gets OFI-heavy weights (0.45/0.40/0.15)
///   5. horizon_cap     — Cap remaining_secs at 600 in drift z-score
///   6. combined        — Ideas 1 + 2 + 4 + 5 together
///
/// Usage: cargo run --release -- --db-path ../../data/polymarket_btc_data.db

use anyhow::Result;
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use rayon::prelude::*;
use rusqlite::Connection;
use serde::Serialize;
use std::collections::HashSet;
use std::path::PathBuf;
use prettytable::{Table, row};

// ─────────────────────────────────────────────────────────────────
// CLI
// ─────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(about = "V12 Binance-Only Directional Accuracy Backtester")]
struct Args {
    #[arg(long, default_value = "../../data/polymarket_btc_data.db")]
    db_path: PathBuf,

    /// Starting bankroll for simulated P&L
    #[arg(long, default_value_t = 100.0)]
    bankroll: f64,

    /// Bet fraction (2% default, Binance-only sim uses assumed $0.50 entry)
    #[arg(long, default_value_t = 0.02)]
    bet_fraction: f64,
}

// ─────────────────────────────────────────────────────────────────
// V12 CONFIG
// ─────────────────────────────────────────────────────────────────

const SLIPPAGE: f64      = 0.005;
const FEE_RATE: f64      = 0.01;
const ASSUMED_ASK: f64   = 0.50; // fair-value assumed entry (no Polymarket ticks)

const MIN_SECS: i64      = 60;
const MAX_SECS: i64      = 600;
const DURATION_SECS: i64 = 900;

const ENTRY_CONF: f64    = 0.55;
const MIN_EDGE: f64      = 0.05;

const BASE_WINDOW: i64   = 25;
const MIN_WINDOW: i64    = 12;
const MAX_WINDOW: i64    = 40;
const TYPICAL_VOL: f64   = 0.0002;

const TREND_THRESH: f64  = 0.15;
const CHOP_THRESH: f64   = 0.06;
const AUTOCORR_CHOP: f64 = -0.25;
const REGIME_LB: usize   = 60;
const NEUTRAL_PEN: f64   = 0.02;

const N_VARIANTS: usize  = 7;
const VARIANT_NAMES: [&str; N_VARIANTS] = [
    "baseline_v12",
    "recency_drift",
    "multiwindow_ofi",
    "momentum_accel",
    "regime_weights",
    "horizon_cap",
    "combined",
];

// ─────────────────────────────────────────────────────────────────
// DATA STRUCTURES
// ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
struct Trade {
    time_ms:         i64,
    price:           f64,
    qty:             f64,
    is_buyer_maker:  bool,
}

#[derive(Debug, Clone)]
struct MarketMeta {
    slug:    String,
    epoch_s: i64,
}

#[derive(Debug, Clone, PartialEq)]
enum Regime { Trend, Neutral, Chop }

#[derive(Debug, Clone, Copy, Default)]
struct VResult {
    fired:             bool,
    correct:           bool,
    secs_in:           i64,
    confidence:        f64,
    pnl:               f64,
    regime:            u8, // 0=trend 1=neutral 2=chop/unfired
    // Feature snapshot at fire time (baseline v=0 only, others zero)
    drift_prob_up:     f64,
    ofi_accel_signal:  f64,
    scoreboard_signal: f64,
    path_eff:          f64,
    autocorr:          f64,
    vol_1s:            f64,
    actual_up:         u8, // 1=UP 0=DOWN
}

#[derive(Debug, Default)]
struct VSummary {
    evaluated: usize,
    fired:     usize,
    wins:      usize,
    trend_fired: usize, trend_wins: usize,
    neut_fired:  usize, neut_wins:  usize,
    conf_sum:  f64,
    secs_sum:  i64,
    pnl_sum:   f64,
}

#[derive(Debug, Serialize)]
struct TradeRow {
    variant: String,
    slug: String,
    secs_in: i64,
    correct: bool,
    confidence: f64,
    pnl: f64,
}

/// ML training row — one per baseline signal fire
#[derive(Debug, Serialize)]
struct FeatureRow {
    epoch_s:           i64,
    secs_in:           i64,
    remaining_secs:    i64,
    drift_prob_up:     f64,
    ofi_accel_signal:  f64,
    scoreboard_signal: f64,
    path_eff:          f64,
    autocorr:          f64,
    vol_1s:            f64,
    confidence:        f64,
    regime:            u8,
    actual_up:         u8,
    correct:           u8,
}

// ─────────────────────────────────────────────────────────────────
// BLACKLIST (v10 heatmap — by (dow, et_hour))
// ─────────────────────────────────────────────────────────────────

fn build_blacklist() -> HashSet<(u32, u32)> {
    let mut s = HashSet::new();
    // Global bad hours all days
    for dow in 0..7u32 {
        for &h in &[0u32, 9, 10, 15, 16] { s.insert((dow, h)); }
    }
    // Day-specific cuts (<60% WR in backtest)
    for (d, h) in [
        (0,13),(0,18),(0,20),
        (1,3),(1,5),(1,6),(1,7),(1,8),(1,18),(1,21),(1,23),
        (2,7),(2,13),(2,18),(2,22),
        (3,6),(3,19),(3,23),
        (4,7),(4,12),(4,13),(4,14),(4,17),(4,18),(4,19),(4,23),
        (5,3),(5,5),(5,6),(5,21),(5,23),
        (6,1),(6,3),(6,20),(6,22),(6,23),
    ] { s.insert((d, h)); }
    s
}

fn is_blacklisted(epoch_s: i64, bl: &HashSet<(u32, u32)>) -> bool {
    // Rough EST (no DST) — good enough for backtesting
    let et_hour = ((epoch_s / 3600 % 24) - 5).rem_euclid(24) as u32;
    let dow = (((epoch_s - 5 * 3600) / 86400 + 3) % 7) as u32;
    bl.contains(&(dow, et_hour))
}

// ─────────────────────────────────────────────────────────────────
// MATH PRIMITIVES
// ─────────────────────────────────────────────────────────────────

#[inline] fn sigmoid(x: f64, scale: f64) -> f64 {
    let z = -x * scale;
    if z > 500.0 { 0.0 } else if z < -500.0 { 1.0 } else { 1.0 / (1.0 + z.exp()) }
}

fn norm_cdf(x: f64) -> f64 {
    if x < 0.0 { return 1.0 - norm_cdf(-x); }
    let t = 1.0 / (1.0 + 0.2316419 * x);
    let pdf = (-x * x / 2.0).exp() / (2.0 * std::f64::consts::PI).sqrt();
    1.0 - pdf * t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
}

fn pearson_corr(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len() as f64;
    let ma = a.iter().sum::<f64>() / n;
    let mb = b.iter().sum::<f64>() / n;
    let (mut cov, mut va, mut vb) = (0.0, 0.0, 0.0);
    for i in 0..a.len() {
        let da = a[i]-ma; let db = b[i]-mb;
        cov += da*db; va += da*da; vb += db*db;
    }
    let den = (va*vb).sqrt();
    if den < 1e-15 { 0.0 } else { cov / den }
}

// ─────────────────────────────────────────────────────────────────
// REGIME DETECTION
// ─────────────────────────────────────────────────────────────────

fn detect_regime(close: &[f64], up_to: usize) -> (Regime, f64, f64) {
    let end = up_to.min(close.len());
    let start = if end > REGIME_LB { end - REGIME_LB } else { 0 };
    let valid: Vec<f64> = close[start..end].iter().copied().filter(|&p| p > 0.0).collect();
    if valid.len() < 15 { return (Regime::Neutral, 0.0, 0.0); }

    let direct = (valid.last().unwrap() - valid.first().unwrap()).abs();
    let total_path: f64 = valid.windows(2).map(|w| (w[1]-w[0]).abs()).sum();
    let path_eff = direct / (total_path + 1e-12);

    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/(w[0]+1e-9)).ln()).collect();
    let autocorr = if rets.len() > 5 {
        let r = pearson_corr(&rets[..rets.len()-1], &rets[1..]);
        if r.is_nan() { 0.0 } else { r }
    } else { 0.0 };

    if autocorr < AUTOCORR_CHOP { return (Regime::Chop, path_eff, autocorr); }
    if path_eff >= TREND_THRESH && autocorr > -0.10 { return (Regime::Trend, path_eff, autocorr); }
    if path_eff < CHOP_THRESH { return (Regime::Chop, path_eff, autocorr); }
    (Regime::Neutral, path_eff, autocorr)
}

fn adaptive_confirm(close: &[f64], up_to: usize) -> i64 {
    let end = up_to.min(close.len());
    let valid: Vec<f64> = close[..end].iter().copied().filter(|&p| p > 0.0).collect();
    if valid.len() < 10 { return BASE_WINDOW; }
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/(w[0]+1e-9)).ln()).collect();
    let recent = if rets.len() > 30 { &rets[rets.len()-30..] } else { &rets[..] };
    let vol = if recent.len() > 3 {
        let m = recent.iter().sum::<f64>() / recent.len() as f64;
        (recent.iter().map(|r| (r-m).powi(2)).sum::<f64>() / recent.len() as f64).sqrt()
    } else { 0.0 };
    let vol_score = (vol / TYPICAL_VOL).min(2.0);
    ((BASE_WINDOW as f64 * (1.3 - 0.3*vol_score).max(0.5)) as i64)
        .max(MIN_WINDOW).min(MAX_WINDOW)
}

/// Compute recent 1s log-return volatility (last 30 bars)
fn compute_vol_1s(close: &[f64], up_to: usize) -> f64 {
    let valid: Vec<f64> = close[..up_to].iter().copied().filter(|&p| p > 0.0).collect();
    if valid.len() < 10 { return 0.0; }
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/(w[0]+1e-9)).ln()).collect();
    let recent = if rets.len() > 30 { &rets[rets.len()-30..] } else { &rets[..] };
    if recent.len() < 4 { return 0.0; }
    let m = recent.iter().sum::<f64>() / recent.len() as f64;
    (recent.iter().map(|r| (r-m).powi(2)).sum::<f64>() / recent.len() as f64).sqrt()
}

// ─────────────────────────────────────────────────────────────────
// SHARED BUILDING BLOCKS
// ─────────────────────────────────────────────────────────────────

/// Compute uniform-weighted drift prob_up
fn drift_uniform(close: &[f64], up_to: usize, remaining: i64) -> f64 {
    let valid: Vec<f64> = close[..up_to].iter().copied().filter(|&p| p > 0.0).collect();
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/w[0]).ln()).collect();
    if rets.len() < 5 { return 0.5; }
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r-mu).powi(2)).sum::<f64>() / rets.len() as f64;
    let sigma = var.sqrt();
    if sigma < 1e-15 || remaining <= 0 { return 0.5; }
    norm_cdf(mu * (remaining as f64).sqrt() / sigma)
}

/// Recency-weighted drift (lambda=0.05 decay)
fn drift_recency(close: &[f64], up_to: usize, remaining: i64) -> f64 {
    let valid: Vec<f64> = close[..up_to].iter().copied().filter(|&p| p > 0.0).collect();
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/w[0]).ln()).collect();
    if rets.len() < 5 { return 0.5; }
    let lambda = 0.05f64;
    let n = rets.len();
    let weights: Vec<f64> = (0..n).map(|i| (-lambda * (n-1-i) as f64).exp()).collect();
    let ws: f64 = weights.iter().sum();
    let mu = rets.iter().zip(&weights).map(|(r,w)| r*w).sum::<f64>() / ws;
    let var = rets.iter().zip(&weights).map(|(r,w)| w*(r-mu).powi(2)).sum::<f64>() / ws;
    let sigma = var.sqrt();
    if sigma < 1e-15 || remaining <= 0 { return 0.5; }
    norm_cdf(mu * (remaining as f64).sqrt() / sigma)
}

/// Drift with horizon capped at 600s
fn drift_horizon_cap(close: &[f64], up_to: usize, remaining: i64) -> f64 {
    let valid: Vec<f64> = close[..up_to].iter().copied().filter(|&p| p > 0.0).collect();
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/w[0]).ln()).collect();
    if rets.len() < 5 { return 0.5; }
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r-mu).powi(2)).sum::<f64>() / rets.len() as f64;
    let sigma = var.sqrt();
    if sigma < 1e-15 || remaining <= 0 { return 0.5; }
    let eff = (remaining as f64).min(600.0);
    norm_cdf(mu * eff.sqrt() / sigma)
}

/// 2-window OFI acceleration (half-split)
fn ofi_2window(buy: &[f64], sell: &[f64], up_to: usize) -> f64 {
    let half = (up_to / 2).max(5);
    let br: f64 = buy[up_to.saturating_sub(half)..up_to].iter().sum();
    let sr: f64 = sell[up_to.saturating_sub(half)..up_to].iter().sum();
    let be: f64 = buy[..half.min(up_to)].iter().sum();
    let se: f64 = sell[..half.min(up_to)].iter().sum();
    let ofi_r = (br - sr) / (br + sr + 1e-9);
    let ofi_e = (be - se) / (be + se + 1e-9);
    sigmoid(ofi_r - ofi_e, 3.0)
}

/// 3-window OFI: last 30s / 90s / all
fn ofi_3window(buy: &[f64], sell: &[f64], up_to: usize) -> f64 {
    let w30 = 30usize.min(up_to);
    let w90 = 90usize.min(up_to);
    let b30: f64 = buy[up_to.saturating_sub(w30)..up_to].iter().sum();
    let s30: f64 = sell[up_to.saturating_sub(w30)..up_to].iter().sum();
    let b90: f64 = buy[up_to.saturating_sub(w90)..up_to].iter().sum();
    let s90: f64 = sell[up_to.saturating_sub(w90)..up_to].iter().sum();
    let ball: f64 = buy[..up_to].iter().sum();
    let sall: f64 = sell[..up_to].iter().sum();
    let ofi30 = (b30 - s30) / (b30 + s30 + 1e-9);
    let ofi90 = (b90 - s90) / (b90 + s90 + 1e-9);
    let ofiall = (ball - sall) / (ball + sall + 1e-9);
    sigmoid(0.6 * (ofi30 - ofi90) + 0.4 * (ofi90 - ofiall), 3.0)
}

/// Scoreboard: price vs open
fn scoreboard(close: &[f64], up_to: usize, open_price: f64) -> f64 {
    let cur = close[..up_to].iter().copied().filter(|&p| p > 0.0).last().unwrap_or(open_price);
    sigmoid((cur - open_price) / (open_price + 1e-9), 1000.0)
}

/// Momentum acceleration: (return over last 30s) - (return over last 90s)
fn momentum_accel_signal(close: &[f64], up_to: usize) -> f64 {
    if up_to < 30 { return 0.5; }
    let cur = close[..up_to].iter().copied().filter(|&p| p > 0.0).last().unwrap_or(0.0);
    if cur <= 0.0 { return 0.5; }
    let p30 = close[..up_to.saturating_sub(30)].iter().copied().filter(|&p| p > 0.0).last().unwrap_or(cur);
    let p90 = close[..up_to.saturating_sub(90.min(up_to))].iter().copied().filter(|&p| p > 0.0).last().unwrap_or(cur);
    let ret30 = (cur - p30) / (p30 + 1e-9);
    let ret90 = (cur - p90) / (p90 + 1e-9);
    sigmoid(ret30 - ret90, 5000.0)
}

/// Combine components into (direction, confidence)
fn combine(drift: f64, ofi: f64, score: f64, extra: Option<f64>, weights: (f64,f64,f64,f64), regime: &Regime) -> (String, f64) {
    let (wd, wo, ws, we) = weights;
    let base = wd*drift + wo*ofi + ws*score + we*extra.unwrap_or(0.5);
    let (dir, mut conf) = if base > 0.5 { ("UP".to_string(), base) } else { ("DOWN".to_string(), 1.0-base) };
    if *regime == Regime::Neutral { conf -= NEUTRAL_PEN; }
    (dir, conf)
}

// ─────────────────────────────────────────────────────────────────
// SIGNAL VARIANTS
// ─────────────────────────────────────────────────────────────────

fn compute_signal(v: usize, close: &[f64], buy: &[f64], sell: &[f64],
                  open: f64, remaining: i64, up_to: usize, regime: &Regime) -> Option<(String, f64)> {
    if up_to < 15 { return None; }
    let valid_count = close[..up_to].iter().filter(|&&p| p > 0.0).count();
    if valid_count < 15 { return None; }

    match v {
        // 0: baseline — uniform drift, 2-window OFI, 0.55/0.30/0.15
        0 => {
            let d = drift_uniform(close, up_to, remaining);
            let o = ofi_2window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            Some(combine(d, o, sc, None, (0.55, 0.30, 0.15, 0.0), regime))
        }
        // 1: recency_drift — exponentially-decayed drift
        1 => {
            let d = drift_recency(close, up_to, remaining);
            let o = ofi_2window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            Some(combine(d, o, sc, None, (0.55, 0.30, 0.15, 0.0), regime))
        }
        // 2: multiwindow_ofi — 3-window OFI
        2 => {
            let d = drift_uniform(close, up_to, remaining);
            let o = ofi_3window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            Some(combine(d, o, sc, None, (0.55, 0.30, 0.15, 0.0), regime))
        }
        // 3: momentum_accel — 4th component (0.50/0.27/0.13/0.10)
        3 => {
            if up_to < 30 { return None; }
            let d = drift_uniform(close, up_to, remaining);
            let o = ofi_2window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            let mom = momentum_accel_signal(close, up_to);
            Some(combine(d, o, sc, Some(mom), (0.50, 0.27, 0.13, 0.10), regime))
        }
        // 4: regime_weights — Neutral uses OFI-heavy (0.45/0.40/0.15)
        4 => {
            let d = drift_uniform(close, up_to, remaining);
            let o = ofi_2window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            let (wd, wo) = match regime {
                Regime::Trend   => (0.55, 0.30),
                Regime::Neutral => (0.45, 0.40),
                Regime::Chop    => return None,
            };
            Some(combine(d, o, sc, None, (wd, wo, 0.15, 0.0), regime))
        }
        // 5: horizon_cap — remaining_secs capped at 600
        5 => {
            let d = drift_horizon_cap(close, up_to, remaining);
            let o = ofi_2window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            Some(combine(d, o, sc, None, (0.55, 0.30, 0.15, 0.0), regime))
        }
        // 6: combined — recency drift + 3-window OFI + regime weights + horizon cap
        6 => {
            let d = drift_recency(close, up_to, remaining.min(600));
            let o = ofi_3window(buy, sell, up_to);
            let sc = scoreboard(close, up_to, open);
            let (wd, wo) = match regime {
                Regime::Trend   => (0.55, 0.30),
                Regime::Neutral => (0.45, 0.40),
                Regime::Chop    => return None,
            };
            Some(combine(d, o, sc, None, (wd, wo, 0.15, 0.0), regime))
        }
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────────
// CONFIRMATION STATE
// ─────────────────────────────────────────────────────────────────

#[derive(Default)]
struct ConfState { dir: Option<String>, count: i64 }

impl ConfState {
    fn update(&mut self, dir: &str, conf: f64, window: i64) -> bool {
        if conf < ENTRY_CONF { return false; }
        if self.dir.as_deref() == Some(dir) { self.count += 1; }
        else { self.dir = Some(dir.to_string()); self.count = 1; }
        self.count >= window
    }
    fn reset(&mut self) { self.dir = None; self.count = 0; }
}

// ─────────────────────────────────────────────────────────────────
// MARKET SIMULATION
// ─────────────────────────────────────────────────────────────────

fn simulate_market(
    close: &[f64; 900], buy: &[f64; 900], sell: &[f64; 900],
    open: f64, actual: &str,
    blacklisted: bool,
    bankroll: f64, bet_fraction: f64,
) -> [VResult; N_VARIANTS] {
    let mut results: [VResult; N_VARIANTS] = Default::default();
    if blacklisted { return results; }

    let mut states: [ConfState; N_VARIANTS] = Default::default();
    let mut fired = [false; N_VARIANTS];

    for s in MIN_SECS as usize..MAX_SECS as usize {
        if fired.iter().all(|&f| f) { break; }

        let up_to = s + 1;
        let remaining = DURATION_SECS - s as i64;
        let (regime, _, _) = detect_regime(close, up_to);
        let window = adaptive_confirm(close, up_to);

        for v in 0..N_VARIANTS {
            if fired[v] { continue; }

            if regime == Regime::Chop {
                states[v].reset();
                continue;
            }

            let Some((dir, conf)) = compute_signal(v, close, buy, sell, open, remaining, up_to, &regime) else {
                continue;
            };

            if states[v].update(&dir, conf, window) {
                let entry_price = ASSUMED_ASK + SLIPPAGE;
                let edge = conf - entry_price;
                if edge < MIN_EDGE {
                    states[v].reset();
                    continue;
                }

                fired[v] = true;
                let correct = dir == actual;
                let bet = bankroll * bet_fraction;
                let shares = (bet * (1.0 - FEE_RATE)) / entry_price;
                let pnl = if correct {
                    shares * 1.0 * (1.0 - FEE_RATE) - bet
                } else { -bet };

                let reg_u8 = match regime { Regime::Trend => 0u8, Regime::Neutral => 1, _ => 2 };
                results[v] = VResult {
                    fired: true, correct,
                    secs_in: s as i64,
                    confidence: conf,
                    pnl,
                    regime: reg_u8,
                    actual_up: if actual == "UP" { 1 } else { 0 },
                    ..Default::default()
                };
                // Capture full feature snapshot for baseline only
                if v == 0 {
                    let (_, peff, acorr) = detect_regime(close, up_to);
                    results[0].drift_prob_up     = drift_uniform(close, up_to, remaining);
                    results[0].ofi_accel_signal  = ofi_2window(buy, sell, up_to);
                    results[0].scoreboard_signal = scoreboard(close, up_to, open);
                    results[0].path_eff          = peff;
                    results[0].autocorr          = acorr;
                    results[0].vol_1s            = compute_vol_1s(close, up_to);
                }
            } // end if states[v].update
        } // end for v
    } // end for s
    results
}

// ─────────────────────────────────────────────────────────────────
// DATA LOADING
// ─────────────────────────────────────────────────────────────────

fn load_data(db_path: &PathBuf) -> Result<(Vec<MarketMeta>, Vec<Trade>)> {
    let conn = Connection::open(db_path)?;
    println!("  Loading market_meta...");
    let mut stmt = conn.prepare("SELECT market_slug, first_seen_ms FROM market_meta ORDER BY first_seen_ms ASC")?;
    let meta: Vec<MarketMeta> = stmt.query_map([], |row| {
        let slug: String = row.get(0)?;
        let first_seen: i64 = row.get(1)?;
        let epoch_s = slug.split('-').last()
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(first_seen / 1000);
        Ok(MarketMeta { slug, epoch_s })
    })?.filter_map(|r| r.ok()).collect();

    println!("  Loading binance_trades...");
    let mut stmt = conn.prepare(
        "SELECT trade_time, price, quantity, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC"
    )?;
    let trades: Vec<Trade> = stmt.query_map([], |row| {
        Ok(Trade {
            time_ms: row.get::<_, i64>(0)?,
            price: row.get(1)?,
            qty: row.get(2)?,
            is_buyer_maker: row.get::<_, i32>(3)? == 1,
        })
    })?.filter_map(|r| r.ok()).collect();

    println!("  Loaded {} markets, {} trades", meta.len(), trades.len());
    Ok((meta, trades))
}

// ─────────────────────────────────────────────────────────────────
// BAR BUILDER
// ─────────────────────────────────────────────────────────────────

fn build_bars(trades: &[Trade], start_ms: i64) -> ([f64; 900], [f64; 900], [f64; 900], f64) {
    let mut close = [0.0f64; 900];
    let mut buy_v = [0.0f64; 900];
    let mut sell_v = [0.0f64; 900];

    let mut open_price = 0.0f64;
    for t in trades {
        let sec = ((t.time_ms - start_ms) / 1000).clamp(0, 899) as usize;
        close[sec] = t.price;
        if t.is_buyer_maker { sell_v[sec] += t.qty; } else { buy_v[sec] += t.qty; }
        if open_price == 0.0 { open_price = t.price; }
    }
    // Forward-fill
    let mut cur = open_price;
    for c in close.iter_mut() {
        if *c > 0.0 { cur = *c; } else { *c = cur; }
    }
    (close, buy_v, sell_v, open_price)
}

// ─────────────────────────────────────────────────────────────────
// OUTPUT
// ─────────────────────────────────────────────────────────────────

fn print_results(summaries: &[VSummary], total_markets: usize, bankroll: f64) {
    println!("\n═══════════════════════════════════════════════════════════");
    println!(" V12 BACKTESTER RESULTS  ({} total markets evaluated)", total_markets);
    println!("═══════════════════════════════════════════════════════════");
    println!(" Simulated P&L: assumed entry_ask=$0.50, bet={}% of ${:.0}", 
             summaries[0].fired as f64 / summaries[0].evaluated.max(1) as f64 * 0.0, bankroll);
    println!();

    // Main comparison table
    let mut table = Table::new();
    table.add_row(row!["Variant", "Fired", "Win%", "ΔWR vs Base", "Avg Conf", "Avg Secs", "Sim P&L"]);

    let base_wr = if summaries[0].fired > 0 {
        summaries[0].wins as f64 / summaries[0].fired as f64
    } else { 0.0 };

    for (i, s) in summaries.iter().enumerate() {
        let wr = if s.fired > 0 { s.wins as f64 / s.fired as f64 } else { 0.0 };
        let delta = if i == 0 { "  —  ".to_string() } else { format!("{:+.2}%", (wr - base_wr) * 100.0) };
        let avg_conf = if s.fired > 0 { s.conf_sum / s.fired as f64 } else { 0.0 };
        let avg_secs = if s.fired > 0 { s.secs_sum / s.fired as i64 } else { 0 };
        table.add_row(row![
            VARIANT_NAMES[i],
            format!("{} ({:.1}%)", s.fired, s.fired as f64 / s.evaluated.max(1) as f64 * 100.0),
            format!("{:.2}%", wr * 100.0),
            delta,
            format!("{:.3}", avg_conf),
            format!("{}s", avg_secs),
            format!("${:+.2}", s.pnl_sum),
        ]);
    }
    table.printstd();

    // Regime breakdown for baseline
    let b = &summaries[0];
    println!("\n── Regime Breakdown (baseline_v12) ──");
    let mut rtable = Table::new();
    rtable.add_row(row!["Regime", "Trades", "Win Rate"]);
    if b.trend_fired > 0 {
        rtable.add_row(row!["Trend", b.trend_fired, format!("{:.2}%", b.trend_wins as f64 / b.trend_fired as f64 * 100.0)]);
    }
    if b.neut_fired > 0 {
        rtable.add_row(row!["Neutral", b.neut_fired, format!("{:.2}%", b.neut_wins as f64 / b.neut_fired as f64 * 100.0)]);
    }
    rtable.printstd();

    println!("\n⭐ Best variant by win rate: {}",
        summaries.iter().enumerate()
            .max_by(|(_, a), (_, b)| {
                let wa = if a.fired > 0 { a.wins as f64 / a.fired as f64 } else { 0.0 };
                let wb = if b.fired > 0 { b.wins as f64 / b.fired as f64 } else { 0.0 };
                wa.partial_cmp(&wb).unwrap()
            })
            .map(|(i, _)| VARIANT_NAMES[i])
            .unwrap_or("?")
    );
}

// ─────────────────────────────────────────────────────────────────
// MAIN
// ─────────────────────────────────────────────────────────────────

fn main() -> Result<()> {
    let args = Args::parse();

    println!("════════════════════════════════════════════════════════");
    println!(" V12 BINANCE-ONLY DIRECTIONAL ACCURACY BACKTESTER");
    println!(" 7 signal variants × 60 days of BTC data");
    println!("════════════════════════════════════════════════════════\n");

    let (meta, trades) = load_data(&args.db_path)?;
    let blacklist = build_blacklist();

    let pb = ProgressBar::new(meta.len() as u64);
    pb.set_style(ProgressStyle::default_bar()
        .template("{spinner:.green} [{bar:45.cyan/blue}] {pos}/{len} ({eta})")?
        .progress_chars("█▉▊▋▌▍▎▏  "));

    // Per-market results: [n_markets][N_VARIANTS]
    let all_results: Vec<([VResult; N_VARIANTS], String)> = meta.par_iter().map(|m| {
        pb.inc(1);
        let start_ms = m.epoch_s * 1000;
        let end_ms = start_ms + DURATION_SECS * 1000;

        // Slice trades for this market
        let lo = trades.partition_point(|t| t.time_ms < start_ms);
        let hi = trades.partition_point(|t| t.time_ms < end_ms);
        let mkt_trades = &trades[lo..hi];

        if mkt_trades.len() < 50 {
            return ([Default::default(); N_VARIANTS], m.slug.clone());
        }

        // Settle direction: first trade AFTER window (v10 fix)
        let settle_idx = trades.partition_point(|t| t.time_ms < end_ms);
        let btc_start = mkt_trades[0].price;
        let btc_end = if settle_idx < trades.len() {
            trades[settle_idx].price
        } else {
            mkt_trades.last().unwrap().price
        };
        let actual = if btc_end > btc_start { "UP" } else { "DOWN" };

        let blacklisted = is_blacklisted(m.epoch_s, &blacklist);
        let (close, buy_v, sell_v, open) = build_bars(mkt_trades, start_ms);

        let results = simulate_market(
            &close, &buy_v, &sell_v,
            open, actual, blacklisted,
            args.bankroll, args.bet_fraction,
        );
        (results, m.slug.clone())
    }).collect();

    pb.finish_with_message("Done simulating");

    // Aggregate summaries
    let total_markets = all_results.len();
    let mut summaries: [VSummary; N_VARIANTS] = Default::default();

    for (variant_results, _slug) in &all_results {
        // A market is "evaluated" if at least baseline fired an attempt
        // Actually count all non-blacklisted markets
        for v in 0..N_VARIANTS {
            summaries[v].evaluated += 1;
            let r = &variant_results[v];
            if r.fired {
                summaries[v].fired += 1;
                summaries[v].conf_sum += r.confidence;
                summaries[v].secs_sum += r.secs_in;
                summaries[v].pnl_sum += r.pnl;
                if r.correct { summaries[v].wins += 1; }
                match r.regime {
                    0 => { summaries[v].trend_fired += 1; if r.correct { summaries[v].trend_wins += 1; } }
                    1 => { summaries[v].neut_fired  += 1; if r.correct { summaries[v].neut_wins  += 1; } }
                    _ => {}
                }
            }
        }
    }

    print_results(&summaries, total_markets, args.bankroll);

    // Save CSV trade log (baseline only)
    let csv_path = "v12_trade_log.csv";
    let mut wtr = csv::Writer::from_path(csv_path)?;
    for (variant_results, slug) in &all_results {
        for (v, r) in variant_results.iter().enumerate() {
            if r.fired {
                wtr.serialize(TradeRow {
                    variant: VARIANT_NAMES[v].to_string(),
                    slug: slug.clone(),
                    secs_in: r.secs_in,
                    correct: r.correct,
                    confidence: r.confidence,
                    pnl: r.pnl,
                })?;
            }
        }
    }
    wtr.flush()?;
    println!("\n  Trade log saved → {}", csv_path);

    // ── Feature CSV for ML Phase 1 ──
    let feat_path = "v12_features.csv";
    let mut fwtr = csv::Writer::from_path(feat_path)?;
    for (variant_results, slug) in &all_results {
        let r = &variant_results[0]; // baseline only
        if r.fired {
            let epoch_s = slug.split('-').last()
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            fwtr.serialize(FeatureRow {
                epoch_s,
                secs_in:           r.secs_in,
                remaining_secs:    DURATION_SECS - r.secs_in,
                drift_prob_up:     r.drift_prob_up,
                ofi_accel_signal:  r.ofi_accel_signal,
                scoreboard_signal: r.scoreboard_signal,
                path_eff:          r.path_eff,
                autocorr:          r.autocorr,
                vol_1s:            r.vol_1s,
                confidence:        r.confidence,
                regime:            r.regime,
                actual_up:         r.actual_up,
                correct:           if r.correct { 1 } else { 0 },
            })?;
        }
    }
    fwtr.flush()?;
    println!("  Feature CSV → {} ({} rows — run: python3 ml_phase1.py)",
        feat_path,
        all_results.iter().filter(|(vr, _)| vr[0].fired).count());

    Ok(())
}
