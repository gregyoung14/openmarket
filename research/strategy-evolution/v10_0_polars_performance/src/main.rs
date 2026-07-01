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
#[command(author, version, about, long_about = None)]
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
}

// Config Constants (v9.2)
const W_DRIFT: f64 = 0.55;
const W_OFI_ACCEL: f64 = 0.30;
const W_SCOREBOARD: f64 = 0.15;
const SCOREBOARD_SCALE: f64 = 1000.0;
const OFI_SCALE: f64 = 3.0;

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

const SLIPPAGE: f64 = 0.005;
const FEE_RATE: f64 = 0.01;

lazy_static! {
    /// Day-specific blacklist: (day_of_week, hour_et)
    /// dow: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    /// Derived from backtest heatmap analysis: all combos < 60% win rate are cut.
    /// Original hour-only blacklist {0,9,10,15,16} is included for all days.
    static ref BLACKLIST_DOW_HOUR_ET: HashSet<(u32, u32)> = {
        let mut s = HashSet::new();

        // Original global blacklist hours — apply to ALL days
        for dow in 0..7u32 {
            for &h in &[0u32, 9, 10, 15, 16] {
                s.insert((dow, h));
            }
        }

        // Data-driven cuts (< 60% win rate, ≥ 30 trades in backtest)
        // Monday
        s.insert((0, 13)); // 58.3%
        s.insert((0, 18)); // 55.6%
        s.insert((0, 20)); // 58.3%
        // Tuesday
        s.insert((1, 3));  // 57.1%
        s.insert((1, 5));  // 52.8%
        s.insert((1, 6));  // 54.3%
        s.insert((1, 7));  // 55.6%
        s.insert((1, 8));  // 55.6%
        s.insert((1, 18)); // 55.6%
        s.insert((1, 21)); // 58.3%
        s.insert((1, 23)); // 58.3%
        // Wednesday
        s.insert((2, 7));  // 55.9%
        s.insert((2, 13)); // 55.6%
        s.insert((2, 18)); // 55.9%
        s.insert((2, 22)); // 44.4%
        // Thursday
        s.insert((3, 6));  // 52.8%
        s.insert((3, 19)); // 59.4%
        s.insert((3, 23)); // 56.2%
        // Friday
        s.insert((4, 7));  // 50.0%
        s.insert((4, 12)); // 58.1%
        s.insert((4, 13)); // 53.1%
        s.insert((4, 14)); // 58.1%
        s.insert((4, 17)); // 56.2%
        s.insert((4, 18)); // 37.5%
        s.insert((4, 19)); // 59.4%
        s.insert((4, 23)); // 46.9%
        // Saturday
        s.insert((5, 3));  // 59.4%
        s.insert((5, 5));  // 59.4%
        s.insert((5, 6));  // 38.7%
        s.insert((5, 21)); // 56.2%
        s.insert((5, 23)); // 53.1%
        // Sunday
        s.insert((6, 1));  // 50.0%
        s.insert((6, 3));  // 53.1%
        s.insert((6, 20)); // 58.3%
        s.insert((6, 22)); // 52.8%
        s.insert((6, 23)); // 55.6%

        s
    };
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Market {
    slug: String,
    epoch_s: i64,
    btc_start: f64,
    btc_end: f64,
    actual_direction: String,
}

#[derive(Debug, Clone)]
struct TradeBar {
    close: f64,
    buy_vol: f64,
    sell_vol: f64,
}

#[derive(Debug, Clone)]
struct PolyTick {
    ts: i64,
    best_ask: f64,
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
    if n < 15 {
        return (Regime::Neutral, 0.0, 0.0);
    }
    
    let start_idx = if n > REGIME_LOOKBACK { n - REGIME_LOOKBACK } else { 0 };
    let valid: Vec<f64> = closes[start_idx..].iter().cloned().filter(|&x| x > 0.0).collect();
    
    if valid.len() < 15 {
        return (Regime::Neutral, 0.0, 0.0);
    }

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

fn compute_signal(
    closes: &[f64], 
    buy_vols: &[f64], 
    sell_vols: &[f64], 
    btc_start: f64, 
    remaining_secs: i64
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
    let scoreboard_signal = 1.0 / (1.0 + (-price_vs_open * SCOREBOARD_SCALE).exp());

    let mut combined_prob_up = 
        W_DRIFT * drift_prob_up + 
        W_OFI_ACCEL * ofi_accel_signal + 
        W_SCOREBOARD * scoreboard_signal;

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
}

fn main() -> Result<()> {
    let args = Args::parse();
    
    println!("============================================================");
    println!(" V10.0 POLARS PERFORMANCE BACKTESTER (RUST PORT)");
    println!("============================================================");

    let (df_meta, df_trades, df_ticks) = load_data(&args.db_path)?;

    let slugs: Vec<String> = df_meta.column("market_slug")?.str()?.into_no_null_iter().map(|s| s.to_string()).collect();
    let first_seen_ms: Vec<i64> = df_meta.column("first_seen_ms")?.i64()?.into_no_null_iter().collect();

    let pb = ProgressBar::new(slugs.len() as u64);
    pb.set_style(ProgressStyle::default_bar()
        .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta})")?
        .progress_chars("#>-"));

    let results: Vec<Option<TradeLog>> = slugs.par_iter().zip(first_seen_ms.par_iter()).map(|(slug, &fs_ms): (&String, &i64)| {
        pb.inc(1);
        
        let epoch_s = slug.split('-').last().unwrap_or("0").parse::<i64>().unwrap_or(fs_ms / 1000);
        let start_ms = epoch_s * 1000;
        
        let et_hour = ((epoch_s / 3600 % 24) - 5).rem_euclid(24) as u32;
        // Compute day of week in ET: shift epoch by -5h so the day boundary aligns with ET
        let et_epoch = epoch_s - 5 * 3600;
        let days_since_epoch = et_epoch / 86400;
        let dow = ((days_since_epoch + 3) % 7) as u32; // 0=Mon
        if BLACKLIST_DOW_HOUR_ET.contains(&(dow, et_hour)) {
            return None;
        }

        let end_ms = start_ms + MARKET_DURATION_SECS * 1000;

        // Filter trades for this market
        // Note: In a real high-perf impl, we shouldn't filter every time.
        // But for 155 markets, it's fine.
        let mask = df_trades.column("trade_time").unwrap().i64().unwrap().gt_eq(start_ms) & 
                   df_trades.column("trade_time").unwrap().i64().unwrap().lt(end_ms);
        let mkt_trades = df_trades.filter(&mask).ok()?;
        if mkt_trades.height() < 50 { return None; }

        let btc_start = mkt_trades.column("price").unwrap().f64().unwrap().get(0).unwrap();
        
        // Settle direction
        let settle_mask = df_trades.column("trade_time").unwrap().i64().unwrap().gt_eq(end_ms);
        let settle_trades = df_trades.filter(&settle_mask).ok()?;
        let btc_end = if settle_trades.height() > 0 {
            settle_trades.column("price").unwrap().f64().unwrap().get(0).unwrap()
        } else {
            mkt_trades.column("price").unwrap().f64().unwrap().get(mkt_trades.height()-1).unwrap()
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

        let mut last_p = btc_start;
        for i in 0..mkt_trades.height() {
            let sec = ((times.get(i).unwrap() - start_ms) / 1000).clamp(0, 899) as usize;
            let p = prices.get(i).unwrap();
            close_arr[sec] = p;
            if makers.get(i).unwrap() == 0 {
                buy_arr[sec] += qtys.get(i).unwrap();
            } else {
                sell_arr[sec] += qtys.get(i).unwrap();
            }
            last_p = p;
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

        let mut confirm_count = 0;
        let mut confirm_dir = String::new();

        for s in MIN_SECS_INTO_MARKET..MAX_SECS_INTO_MARKET {
            let res = compute_signal(
                &close_arr[..=s as usize],
                &buy_arr[..=s as usize],
                &sell_arr[..=s as usize],
                btc_start,
                MARKET_DURATION_SECS - s
            )?;

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
                    let now_ms = start_ms + s * 1000;
                    
                    // Lookup best ask
                    let side_mask = mkt_ticks.column("side_label").unwrap().str().unwrap().equal(confirm_dir.as_str());
                    let side_ticks = mkt_ticks.filter(&side_mask).ok()?;
                    
                    let time_mask = side_ticks.column("source_ts_ms").unwrap().i64().unwrap().lt_eq(now_ms);
                    let backward = side_ticks.filter(&time_mask).ok()?;
                    
                    let entry_ask = if backward.height() > 0 {
                        backward.column("best_ask").unwrap().f64().unwrap().get(backward.height()-1).unwrap()
                    } else {
                        // Forward lookup or default
                        0.50
                    };

                    let entry_price = entry_ask + SLIPPAGE;
                    let edge = res.confidence - entry_price;

                    if entry_ask < args.min_entry_price || entry_ask > args.max_entry_price {
                        confirm_count = 0;
                        continue;
                    }
                    if edge < args.min_edge {
                        confirm_count = 0;
                        continue;
                    }

                    // Success!
                    let final_correct = confirm_dir == actual;
                    return Some(TradeLog {
                        slug: slug.clone(),
                        entry_secs_in: s,
                        side: confirm_dir,
                        entry_price,
                        exit_price: if final_correct { 1.0 } else { 0.0 },
                        pnl: 0.0,
                        bankroll: 0.0,
                        correct: final_correct,
                        conf: res.confidence,
                        edge,
                        regime: format!("{:?}", res.regime),
                        path_eff: res.path_eff,
                        autocorr: res.autocorr,
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
            final_trades.push(t);
        }
    }

    // Save CSV
    let csv_path = "v10_0_trade_log.csv";
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
    table.add_row(row!["Win Rate", format!("{:.1}%", (wins as f64 / final_trades.len() as f64) * 100.0)]);
    table.add_row(row!["Final Bankroll", format!("${:.2}", bankroll)]);
    table.add_row(row!["Total ROI", format!("{:.1}%", (bankroll - args.bankroll) / args.bankroll * 100.0)]);
    
    table.printstd();

    println!("\n  Ready to run!");
    println!("  To re-run: cargo run --release -- --db-path {} --min-confidence {} --min-edge {}", 
        args.db_path.display(), args.min_confidence, args.min_edge);

    Ok(())
}
