/// V8 Window Sweep — CLEAN Polymarket Data Only
///
/// Only runs on markets where polymarket ticks start within 5s of market open.
/// Uses REAL best_ask from order book at signal fire time.
/// Binance trades for signal generation, Polymarket asks for entry pricing + P&L.

use anyhow::Result;
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use rayon::prelude::*;
use rusqlite::Connection;
use std::collections::HashMap;
use std::path::PathBuf;
use prettytable::{Table, row};

#[derive(Parser, Debug)]
#[command(about = "V8 Window Sweep — clean Polymarket overlap only")]
struct Args {
    #[arg(long, default_value = "../../data/polymarket_btc_data.db")]
    db_path: PathBuf,
    #[arg(long, default_value_t = 100.0)]
    bankroll: f64,
    #[arg(long, default_value_t = 0.02)]
    bet_fraction: f64,
}

const WINDOWS: [i64; 15] = [45, 60, 75, 90, 100, 110, 120, 135, 150, 165, 180, 210, 240, 270, 300];
const N: usize = 15;

const SLIPPAGE: f64      = 0.005;
const FEE_RATE: f64      = 0.01;
const MIN_SECS: i64      = 60;
const MAX_SECS: i64      = 780;
const DURATION_SECS: i64 = 900;
const ENTRY_CONF: f64    = 0.55;
const MIN_EDGE: f64      = 0.05;
const REGIME_LB: usize   = 60;
const NEUTRAL_PEN: f64   = 0.02;
const MIN_ENTRY_PRICE: f64 = 0.20; // exclude stub/penny asks with no real depth
const MAX_ENTRY_PRICE: f64 = 0.75;
const W_DRIFT: f64       = 0.45;
const W_SCORE: f64       = 0.25;
const W_OFI: f64         = 0.20;

/// Include all markets that have any polymarket tick data (no first-tick filter)
/// NoAsk column handles missing data per-trade naturally

#[derive(Debug, Clone)]
struct Trade { time_ms: i64, price: f64, qty: f64, is_buyer_maker: bool }

#[derive(Debug, Clone)]
struct MarketMeta { slug: String, epoch_s: i64 }

#[derive(Debug, Clone, PartialEq)]
enum Regime { Trend, Neutral, Chop }

/// Per-second best ask prices for a market
#[derive(Debug, Clone)]
struct MarketAsks {
    up_ask:   [f64; 900],
    down_ask: [f64; 900],
    first_tick_sec: i64, // first second with any ask data
}

#[derive(Debug, Clone, Copy, Default)]
struct VResult {
    fired:      bool,
    correct:    bool,
    secs_in:    i64,
    confidence: f64,
    real_ask:   f64,
    edge:       f64,
    pnl:        f64,
    skipped_price: bool,
    skipped_edge:  bool,
    no_ask_data:   bool,
}

#[derive(Debug, Default, Clone)]
struct Summary {
    evaluated:     usize,
    fired:         usize,
    wins:          usize,
    skipped_price: usize,
    skipped_edge:  usize,
    no_ask:        usize,
    conf_sum:      f64,
    ask_sum:       f64,
    edge_sum:      f64,
    secs_sum:      i64,
    pnl_sum:       f64,
}

// ── Math ──
fn sigmoid(x: f64, scale: f64) -> f64 {
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
    let ma = a.iter().sum::<f64>() / n; let mb = b.iter().sum::<f64>() / n;
    let (mut cov, mut va, mut vb) = (0.0, 0.0, 0.0);
    for i in 0..a.len() { let da=a[i]-ma; let db=b[i]-mb; cov+=da*db; va+=da*da; vb+=db*db; }
    let den = (va*vb).sqrt();
    if den < 1e-15 { 0.0 } else { cov / den }
}
fn compute_whipsaw(closes: &[f64]) -> f64 {
    if closes.len() < 3 { return 0.0; }
    let d: Vec<f64> = closes.windows(2).map(|w| w[1]-w[0]).collect();
    let s: Vec<f64> = d.iter().map(|x| x.signum()).collect();
    let ch = s.windows(2).filter(|w| w[0]!=w[1] && w[0]!=0.0 && w[1]!=0.0).count();
    ch as f64 / (s.len().max(1) - 1).max(1) as f64
}

fn detect_regime(close: &[f64], up_to: usize) -> Regime {
    let end = up_to.min(close.len());
    let start = if end > REGIME_LB { end - REGIME_LB } else { 0 };
    let valid: Vec<f64> = close[start..end].iter().copied().filter(|&p| p>0.0).collect();
    if valid.len() < 15 { return Regime::Neutral; }
    let d = (valid.last().unwrap() - valid.first().unwrap()).abs();
    let tp: f64 = valid.windows(2).map(|w| (w[1]-w[0]).abs()).sum();
    let pe = d / (tp + 1e-12);
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/(w[0]+1e-9)).ln()).collect();
    let ac = if rets.len() > 5 { let r=pearson_corr(&rets[..rets.len()-1],&rets[1..]); if r.is_nan(){0.0}else{r} } else { 0.0 };
    if ac < -0.25 { return Regime::Chop; }
    if pe >= 0.15 && ac > -0.10 { return Regime::Trend; }
    if pe < 0.06 { return Regime::Chop; }
    Regime::Neutral
}

fn compute_v8_signal(
    close: &[f64], buy: &[f64], sell: &[f64],
    open: f64, remaining: i64, up_to: usize, regime: &Regime,
) -> Option<(String, f64)> {
    let valid: Vec<f64> = close[..up_to].iter().copied().filter(|&p| p>0.0).collect();
    if valid.len() < 15 { return None; }
    let cur = *valid.last().unwrap();
    let rets: Vec<f64> = valid.windows(2).map(|w| (w[1]/w[0]).ln()).collect();
    if rets.len() < 5 { return None; }
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r-mu).powi(2)).sum::<f64>() / rets.len() as f64;
    let sigma = var.sqrt();
    let drift = if sigma > 1e-15 && remaining > 0 { norm_cdf(mu*(remaining as f64).sqrt()/sigma) } else { 0.5 };
    let score = sigmoid((cur - open)/(open+1e-9), 1000.0);
    let half = (up_to/2).max(5);
    let br: f64 = buy[up_to.saturating_sub(half)..up_to].iter().sum();
    let sr: f64 = sell[up_to.saturating_sub(half)..up_to].iter().sum();
    let be: f64 = buy[..half.min(up_to)].iter().sum();
    let se: f64 = sell[..half.min(up_to)].iter().sum();
    let ofi = sigmoid((br-sr)/(br+sr+1e-9) - (be-se)/(be+se+1e-9), 3.0);
    let ws = (-(compute_whipsaw(&valid)-0.40).powi(2)/0.08).exp();
    let base = W_DRIFT*drift + W_SCORE*score + W_OFI*ofi;
    let combined = base + 0.10 * if base > 0.5 { ws } else { 1.0-ws };
    let (dir, mut conf) = if combined > 0.5 { ("UP".to_string(), combined) } else { ("DOWN".to_string(), 1.0 - combined) };
    if *regime == Regime::Neutral { conf -= NEUTRAL_PEN; }
    Some((dir, conf))
}

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

/// Forward-fill: get the most recent non-zero ask at or before second `s`
fn get_real_ask(asks: &MarketAsks, dir: &str, s: usize) -> Option<f64> {
    let arr = if dir == "UP" { &asks.up_ask } else { &asks.down_ask };
    for i in (0..=s.min(899)).rev() {
        if arr[i] > 0.0 { return Some(arr[i]); }
    }
    None
}

fn simulate_market(
    close: &[f64; 900], buy: &[f64; 900], sell: &[f64; 900],
    open: f64, actual: &str, asks: &MarketAsks,
    bankroll: f64, bet_fraction: f64,
) -> [VResult; N] {
    let mut results = [VResult::default(); N];
    let mut states: [ConfState; N] = Default::default();
    let mut fired = [false; N];

    for s in MIN_SECS as usize..MAX_SECS as usize {
        if fired.iter().all(|&f| f) { break; }
        let up_to = s + 1;
        let remaining = DURATION_SECS - s as i64;
        if remaining <= 0 { break; }
        let regime = detect_regime(close, up_to);
        let Some((dir, conf)) = compute_v8_signal(close, buy, sell, open, remaining, up_to, &regime) else { continue; };

        for (v, &window) in WINDOWS.iter().enumerate() {
            if fired[v] { continue; }
            if !states[v].update(&dir, conf, window) { continue; }

            let real_ask = get_real_ask(asks, &dir, s);
            let Some(ask) = real_ask else {
                results[v].no_ask_data = true;
                states[v].reset();
                continue;
            };

            if ask > MAX_ENTRY_PRICE || ask < MIN_ENTRY_PRICE {
                results[v].skipped_price = true;
                states[v].reset();
                continue;
            }

            let edge = conf - ask - SLIPPAGE;
            if edge < MIN_EDGE {
                results[v].skipped_edge = true;
                states[v].reset();
                continue;
            }

            fired[v] = true;
            let correct = dir == actual;
            let bet = bankroll * bet_fraction;
            let shares = (bet * (1.0 - FEE_RATE)) / ask;
            let pnl = if correct { shares * (1.0 - FEE_RATE) - bet } else { -bet };

            results[v] = VResult {
                fired: true, correct,
                secs_in: s as i64, confidence: conf,
                real_ask: ask, edge, pnl,
                skipped_price: false, skipped_edge: false, no_ask_data: false,
            };
        }
    }
    results
}

fn load_data(db_path: &PathBuf) -> Result<(Vec<MarketMeta>, Vec<Trade>, HashMap<String, MarketAsks>)> {
    let conn = Connection::open(db_path)?;

    println!("  Loading market_meta...");
    let mut stmt = conn.prepare("SELECT market_slug, first_seen_ms FROM market_meta ORDER BY first_seen_ms ASC")?;
    let meta: Vec<MarketMeta> = stmt.query_map([], |row| {
        let slug: String = row.get(0)?;
        let fs: i64 = row.get(1)?;
        let epoch_s = slug.split('-').last().and_then(|s| s.parse::<i64>().ok()).unwrap_or(fs/1000);
        Ok(MarketMeta { slug, epoch_s })
    })?.filter_map(|r| r.ok()).collect();
    println!("  {} markets total", meta.len());

    println!("  Loading binance_trades...");
    let mut stmt = conn.prepare(
        "SELECT trade_time, price, quantity, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC"
    )?;
    let trades: Vec<Trade> = stmt.query_map([], |row| {
        Ok(Trade { time_ms: row.get::<_,i64>(0)?, price: row.get(1)?, qty: row.get(2)?, is_buyer_maker: row.get::<_,i32>(3)?==1 })
    })?.filter_map(|r| r.ok()).collect();
    println!("  {} binance trades", trades.len());

    println!("  Loading polymarket_ticks_ms...");
    let mut stmt = conn.prepare(
        "SELECT market_slug, side_label, source_ts_ms, best_ask
         FROM polymarket_ticks_ms
         WHERE event_type = 'price_change' AND best_ask > 0
         ORDER BY source_ts_ms ASC"
    )?;

    let mut asks_map: HashMap<String, MarketAsks> = HashMap::new();
    let mut rows = 0u64;

    stmt.query_map([], |row| {
        let slug: String = row.get(0)?;
        let side: String = row.get(1)?;
        let ts_ms: i64 = row.get(2)?;
        let ask: f64 = row.get(3)?;
        Ok((slug, side, ts_ms, ask))
    })?.for_each(|r| {
        if let Ok((slug, side, ts_ms, ask)) = r {
            let epoch_s = slug.split('-').last()
                .and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
            if epoch_s == 0 { return; }
            let sec = ((ts_ms / 1000) - epoch_s).clamp(0, 899) as usize;

            let entry = asks_map.entry(slug).or_insert_with(|| MarketAsks {
                up_ask: [0.0; 900], down_ask: [0.0; 900], first_tick_sec: 999,
            });
            if (sec as i64) < entry.first_tick_sec { entry.first_tick_sec = sec as i64; }
            if side == "UP" { entry.up_ask[sec] = ask; } else { entry.down_ask[sec] = ask; }
            rows += 1;
        }
    });
    println!("  {} polymarket ticks → {} market ask-lookups", rows, asks_map.len());

    Ok((meta, trades, asks_map))
}

fn build_bars(trades: &[Trade], start_ms: i64) -> ([f64; 900], [f64; 900], [f64; 900], f64) {
    let mut close = [0.0f64; 900]; let mut buy_v = [0.0f64; 900]; let mut sell_v = [0.0f64; 900];
    let mut open_price = 0.0f64;
    for t in trades {
        let sec = ((t.time_ms - start_ms)/1000).clamp(0, 899) as usize;
        close[sec] = t.price;
        if t.is_buyer_maker { sell_v[sec] += t.qty; } else { buy_v[sec] += t.qty; }
        if open_price == 0.0 { open_price = t.price; }
    }
    let mut cur = open_price;
    for c in close.iter_mut() { if *c > 0.0 { cur = *c; } else { *c = cur; } }
    (close, buy_v, sell_v, open_price)
}

fn main() -> Result<()> {
    let args = Args::parse();

    println!("══════════════════════════════════════════════════════════════════");
    println!(" V8 WINDOW SWEEP — ALL POLYMARKET OVERLAP MARKETS");
    println!(" REAL ask from polymarket_ticks_ms (UNKNOWN labels fixed)");
    println!(" Windows: {:?}", WINDOWS);
    println!("══════════════════════════════════════════════════════════════════\n");

    let (meta, trades, asks_map) = load_data(&args.db_path)?;

    // All markets that have any polymarket ask data
    let pm_markets: Vec<&MarketMeta> = meta.iter()
        .filter(|m| asks_map.contains_key(&m.slug))
        .collect();

    println!("\n  ✅ {} markets with Polymarket order book data", pm_markets.len());

    let pb = ProgressBar::new(pm_markets.len() as u64);
    pb.set_style(ProgressStyle::default_bar()
        .template("{spinner:.green} [{bar:45.cyan/blue}] {pos}/{len} ({eta})")?
        .progress_chars("█▉▊▋▌▍▎▏  "));

    let empty_asks = MarketAsks { up_ask: [0.0; 900], down_ask: [0.0; 900], first_tick_sec: 999 };

    let all_results: Vec<([VResult; N], String)> = pm_markets.par_iter().map(|m| {
        pb.inc(1);
        let start_ms = m.epoch_s * 1000;
        let end_ms = start_ms + DURATION_SECS * 1000;
        let lo = trades.partition_point(|t| t.time_ms < start_ms);
        let hi = trades.partition_point(|t| t.time_ms < end_ms);
        let mkt = &trades[lo..hi];
        if mkt.len() < 50 { return ([VResult::default(); N], m.slug.clone()); }

        let settle_idx = trades.partition_point(|t| t.time_ms < end_ms);
        let btc_start = mkt[0].price;
        let btc_end = if settle_idx < trades.len() { trades[settle_idx].price } else { mkt.last().unwrap().price };
        let actual = if btc_end > btc_start { "UP" } else { "DOWN" };

        let (close, bv, sv, open) = build_bars(mkt, start_ms);
        let market_asks = asks_map.get(&m.slug).unwrap_or(&empty_asks);
        let results = simulate_market(&close, &bv, &sv, open, actual, market_asks, args.bankroll, args.bet_fraction);
        (results, m.slug.clone())
    }).collect();

    pb.finish_with_message("Done");

    let total = all_results.len();
    let mut sums: [Summary; N] = std::array::from_fn(|_| Summary::default());

    for (vrs, _) in &all_results {
        for v in 0..N {
            sums[v].evaluated += 1;
            let r = &vrs[v];
            if r.skipped_price { sums[v].skipped_price += 1; }
            if r.skipped_edge  { sums[v].skipped_edge += 1; }
            if r.no_ask_data   { sums[v].no_ask += 1; }
            if r.fired {
                sums[v].fired += 1;
                sums[v].wins  += if r.correct { 1 } else { 0 };
                sums[v].conf_sum += r.confidence;
                sums[v].ask_sum  += r.real_ask;
                sums[v].edge_sum += r.edge;
                sums[v].secs_sum += r.secs_in;
                sums[v].pnl_sum  += r.pnl;
            }
        }
    }

    let roi = |v: usize| -> f64 {
        let s = &sums[v];
        if s.fired == 0 { return -99.0; }
        let wr = s.wins as f64 / s.fired as f64;
        let avg_ask = s.ask_sum / s.fired as f64;
        wr * (1.0 - FEE_RATE).powi(2) / avg_ask - 1.0
    };
    let best_roi_v = (0..N).max_by(|&a, &b| roi(a).partial_cmp(&roi(b)).unwrap()).unwrap_or(0);
    let base_v = WINDOWS.iter().position(|&w| w == 45).unwrap_or(0);

    println!("\n════════════════════════════════════════════════════════════════════════════════════════════════");
    println!(" RESULTS — {} clean markets (full Polymarket + Binance coverage)", total);
    println!(" Signal: Binance 1s bars    Entry price: REAL Polymarket best_ask    P&L: real shares/fees");
    println!("════════════════════════════════════════════════════════════════════════════════════════════════");

    let mut table = Table::new();
    table.add_row(row!["Window", "Fired", "Win%", "REAL Ask", "Edge", "Entry", "ROI/trade", "Sim P&L", "Skip$", "SkipEdge", "NoAsk", ""]);

    let base_wr = if sums[base_v].fired > 0 { sums[base_v].wins as f64 / sums[base_v].fired as f64 } else { 0.0 };

    for v in 0..N {
        let s = &sums[v];
        let wr = if s.fired > 0 { s.wins as f64 / s.fired as f64 } else { 0.0 };
        let avg_ask  = if s.fired > 0 { s.ask_sum  / s.fired as f64 } else { 0.0 };
        let avg_edge = if s.fired > 0 { s.edge_sum / s.fired as f64 } else { 0.0 };
        let avg_secs = if s.fired > 0 { s.secs_sum / s.fired as i64 } else { 0 };
        let proj = roi(v);
        let d = (wr - base_wr) * 100.0;
        let flag = if v == best_roi_v { "⭐ BEST" } else if WINDOWS[v] == 45 { "← live" } else { "" };

        table.add_row(row![
            format!("{}s", WINDOWS[v]),
            format!("{}/{}", s.fired, s.evaluated),
            format!("{:.1}% ({:+.1})", wr * 100.0, d),
            format!("${:.3}", avg_ask),
            format!("{:.3}", avg_edge),
            format!("{}s ({:.1}m)", avg_secs, avg_secs as f64 / 60.0),
            format!("{:+.1}%", proj * 100.0),
            format!("${:+.2}", s.pnl_sum),
            format!("{}", s.skipped_price),
            format!("{}", s.skipped_edge),
            format!("{}", s.no_ask),
            flag,
        ]);
    }
    table.printstd();

    // Per-trade log for the best and baseline windows
    println!("\n── Trade Detail: 45s (live) vs {}s (best ROI) ──", WINDOWS[best_roi_v]);
    println!("  {:>4} {:>5} {:>5} {:>7} {:>7} {:>7} {:>8}",
        "Win", "Secs", "Conf", "Ask", "Edge", "P&L", "Slug");
    println!("  {} 45s window:", "─".repeat(60));
    let mut count = 0;
    for (vrs, slug) in &all_results {
        let r = &vrs[base_v];
        if r.fired && count < 15 {
            let mark = if r.correct { "✅" } else { "❌" };
            println!("  {mark} {:>5} {:.3} ${:.3}  {:.3}  ${:+.2}  {}",
                r.secs_in, r.confidence, r.real_ask, r.edge, r.pnl,
                slug.split('-').last().unwrap_or("?"));
            count += 1;
        }
    }

    if best_roi_v != base_v {
        println!("  {} {}s window:", "─".repeat(60), WINDOWS[best_roi_v]);
        count = 0;
        for (vrs, slug) in &all_results {
            let r = &vrs[best_roi_v];
            if r.fired && count < 15 {
                let mark = if r.correct { "✅" } else { "❌" };
                println!("  {mark} {:>5} {:.3} ${:.3}  {:.3}  ${:+.2}  {}",
                    r.secs_in, r.confidence, r.real_ask, r.edge, r.pnl,
                    slug.split('-').last().unwrap_or("?"));
                count += 1;
            }
        }
    }

    println!("\n── Summary ──");
    let best_roi_val = roi(best_roi_v);
    let base_roi = roi(base_v);
    let best_ask = if sums[best_roi_v].fired > 0 { sums[best_roi_v].ask_sum / sums[best_roi_v].fired as f64 } else { 0.0 };
    let base_ask = if sums[base_v].fired > 0 { sums[base_v].ask_sum / sums[base_v].fired as f64 } else { 0.0 };
    println!("  ⭐ Best ROI window: {}s → {:+.1}%/trade, avg ask ${:.3}, {} trades",
        WINDOWS[best_roi_v], best_roi_val * 100.0, best_ask, sums[best_roi_v].fired);
    println!("  📌 Live (45s):      {:+.1}%/trade, avg ask ${:.3}, {} trades",
        base_roi * 100.0, base_ask, sums[base_v].fired);
    println!("  Δ ROI: {:+.1}%/trade", (best_roi_val - base_roi) * 100.0);

    // Total P&L ranking
    println!("\n── Total Sim P&L Ranking (volume × ROI) ──");
    let mut ranked: Vec<(usize, f64)> = (0..N).map(|v| (v, sums[v].pnl_sum)).collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
    for (rank, (v, pnl)) in ranked.iter().enumerate() {
        let s = &sums[*v];
        let wr = if s.fired > 0 { s.wins as f64 / s.fired as f64 } else { 0.0 };
        let flag = if *v == best_roi_v { " ← best ROI" } else if WINDOWS[*v] == 45 { " ← live" } else { "" };
        println!("  #{:<2} {:>4}s: ${:>+8.2} P&L ({} trades, {:.1}% WR){flag}",
            rank+1, WINDOWS[*v], pnl, s.fired, wr * 100.0);
    }

    Ok(())
}
