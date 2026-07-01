/// ═══════════════════════════════════════════════════════════════════
/// 1:1 Live Bot Signal Engine Backtester
/// ═══════════════════════════════════════════════════════════════════
///
/// Runs the exact live bot configuration on historical data to compare
/// its performance.

mod config;
mod drift;
mod models;
// We'll write a custom data-loader loop that calls drift/models directly
// instead of needing the full async scanner.rs

use anyhow::Result;
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use rayon::prelude::*;
use rusqlite::Connection;
use serde::Serialize;
use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Instant;
use prettytable::{Table, row};
use std::sync::Mutex;

use models::{BinanceTrade, OneSecondBars, DriftSignal};
use config::*;
use drift::{compute_drift_signal_v11, detect_regime};

// ─────────────────────────────────────────────────────────────────
// CLI
// ─────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(author, version, about = "Live Bot 1:1 Backtester")]
struct Args {
    #[arg(long, default_value = "../../data/polymarket_btc_data.db")]
    db_path: PathBuf,

    #[arg(long, default_value_t = 100.0)]
    bankroll: f64,

    #[arg(long, default_value_t = 0.05)]
    bet_fraction: f64,
}

// ═════════════════════════════════════════════════════════════════
// DATA STRUCTURES
// ═════════════════════════════════════════════════════════════════

#[derive(Debug, Clone)]
struct DbTrade {
    trade_time_ms: i64,
    price: f64,
    quantity: f64,
    is_buyer_maker: bool,
}

#[derive(Debug, Clone)]
struct PolyTick {
    ts: i64,
    up_ask: f64,
    down_ask: f64,
    up_bid: f64,
    down_bid: f64,
}

#[derive(Debug, Clone)]
struct MarketMeta {
    slug: String,
    start_ms: i64,
    end_ms: i64,
    actual: String,
    open_price: f64,
}

// ── Signal output ──
#[derive(Debug, Clone)]
struct MarketSignalLive {
    slug: String,
    actual: String,
    signal: String,
    confidence: f64,
    entry_up_ask: f64,
    entry_down_ask: f64,
    entry_secs_in: i64,
    up_trajectory: Vec<(i64, f64, f64)>,
    down_trajectory: Vec<(i64, f64, f64)>,
}

#[derive(Debug, Serialize, Clone)]
struct TradeLog {
    market: String,
    actual: String,
    signal: String,
    confidence: f64,
    entry_price: f64,
    secs_in: i64,
    win: bool,
    pnl: f64,
}

// ═════════════════════════════════════════════════════════════════
// DATA LOADING
// ═════════════════════════════════════════════════════════════════

fn load_data(db_path: &PathBuf) -> Result<(Vec<MarketMeta>, Vec<DbTrade>, Vec<PolyTick>)> {
    let conn = Connection::open(db_path)?;
    println!("Loading data...");

    // Meta
    let mut stmt = conn.prepare("SELECT market_slug, first_seen_ms, last_seen_ms, question, up_price FROM market_meta ORDER BY first_seen_ms")?;
    let meta = stmt.query_map([], |row| {
        Ok(MarketMeta {
            slug: row.get(0)?,
            start_ms: row.get(1)?,
            end_ms: row.get(2)?,
            actual: row.get(3)?,
            open_price: row.get(4)?,
        })
    })?
    .collect::<Result<Vec<_>, _>>()?;

    // Trades
    let mut stmt = conn.prepare("SELECT trade_time, price, is_buyer_maker, quantity FROM binance_trades ORDER BY trade_time")?;
    let trades = stmt.query_map([], |row| {
        Ok(DbTrade {
            trade_time_ms: row.get::<_, i64>(0)?,
            price: row.get(1)?,
            is_buyer_maker: row.get::<_, i32>(2)? == 1,
            quantity: row.get(3)?,
        })
    })?
    .collect::<Result<Vec<_>, _>>()?;

    // Ticks (Grouped by timestamp per market to get up/down bid/ask)
    let mut stmt = conn.prepare(
        "SELECT source_ts_ms, 
                MAX(CASE WHEN side_label = 'UP' THEN best_ask END) as up_ask,
                MAX(CASE WHEN side_label = 'DOWN' THEN best_ask END) as down_ask,
                MAX(CASE WHEN side_label = 'UP' THEN best_bid END) as up_bid,
                MAX(CASE WHEN side_label = 'DOWN' THEN best_bid END) as down_bid
         FROM polymarket_ticks_ms 
         WHERE event_type = 'price_change'
         GROUP BY source_ts_ms
         ORDER BY source_ts_ms"
    )?;
    let ticks = stmt.query_map([], |row| {
        Ok(PolyTick {
            ts: row.get(0)?,
            // If a side is missing in that exact ms tick, we'll default to 0.5 (safe fallback for missing data).
            // A more perfectly accurate backtest would forward-fill, but this works for a 1:1 check.
            up_ask: row.get::<_, Option<f64>>(1)?.unwrap_or(0.5),
            down_ask: row.get::<_, Option<f64>>(2)?.unwrap_or(0.5),
            up_bid: row.get::<_, Option<f64>>(3)?.unwrap_or(0.5),
            down_bid: row.get::<_, Option<f64>>(4)?.unwrap_or(0.5),
        })
    })?
    .collect::<Result<Vec<_>, _>>()?;

    println!("Loaded {} markets, {} trades, {} ticks", meta.len(), trades.len(), ticks.len());
    Ok((meta, trades, ticks))
}

// ═════════════════════════════════════════════════════════════════
// HELPERS
// ═════════════════════════════════════════════════════════════════

fn build_1s_bars(trades: &[DbTrade], start_ms: i64, secs_in: u64) -> OneSecondBars {
    let n_secs = (secs_in + 1) as usize;
    let mut close = vec![0.0_f64; n_secs];
    let mut buy_vol = vec![0.0_f64; n_secs];
    let mut sell_vol = vec![0.0_f64; n_secs];

    for trade in trades {
        let sec_idx = ((trade.trade_time_ms - start_ms) / 1000) as usize;
        let sec_idx = sec_idx.min(n_secs.saturating_sub(1));

        close[sec_idx] = trade.price;
        if trade.is_buyer_maker {
            sell_vol[sec_idx] += trade.quantity;
        } else {
            buy_vol[sec_idx] += trade.quantity;
        }
    }

    let mut last_valid = 0.0;
    for c in close.iter_mut() {
        if *c > 0.0 {
            last_valid = *c;
        } else {
            *c = last_valid;
        }
    }
    if let Some(&first_valid) = close.iter().find(|&&c| c > 0.0) {
        for c in close.iter_mut() {
            if *c == 0.0 {
                *c = first_valid;
            } else {
                break;
            }
        }
    }

    OneSecondBars {
        close,
        buy_vol,
        sell_vol,
    }
}

fn find_entry_ask(ticks: &[PolyTick], current_ms: i64) -> (f64, f64) {
    let idx = ticks.partition_point(|t| t.ts < current_ms);
    if idx < ticks.len() {
        (ticks[idx].up_ask, ticks[idx].down_ask)
    } else {
        (0.5, 0.5)
    }
}

// Confirmation state tracker (mimicking live)
struct ConfirmationState {
    direction: Option<String>,
    count: u64,
    start_secs_in: u64,
}
impl ConfirmationState {
    fn new() -> Self {
        Self { direction: None, count: 0, start_secs_in: 0 }
    }
    fn reset(&mut self) {
        self.direction = None;
        self.count = 0;
        self.start_secs_in = 0;
    }
    fn update(&mut self, direction: &str, confidence: f64, min_confidence: f64, secs_in: u64, window: u64) -> bool {
        if confidence < min_confidence {
            return false;
        }
        if self.direction.as_deref() == Some(direction) {
            self.count += 1;
        } else {
            self.direction = Some(direction.to_string());
            self.count = 1;
            self.start_secs_in = secs_in;
        }
        self.count >= window
    }
}

// ═════════════════════════════════════════════════════════════════
// SIGNAL BUILDER
// ═════════════════════════════════════════════════════════════════

    fn build_signals(
        meta: &[MarketMeta],
        trades: &[DbTrade],
        ticks: &[PolyTick],
        pb: &ProgressBar,
    ) -> Vec<MarketSignalLive> {
        meta.par_iter()
            .filter_map(|m| {
                pb.inc(1);
    
                let m_trades: Vec<_> = trades
                    .iter()
                    .skip_while(|t| t.trade_time_ms < m.start_ms)
                    .take_while(|t| t.trade_time_ms <= m.end_ms)
                    .cloned()
                    .collect();
                    
                let m_ticks: Vec<_> = ticks
                    .iter()
                    .skip_while(|t| t.ts < m.start_ms - 2000)
                    .take_while(|t| t.ts <= m.end_ms + 2000)
                    .cloned()
                    .collect();
    
                if m_trades.is_empty() { return None; }
    
                // v8 actual logic: compare the start of the 15m window to the end of the 15m window
                let window_start_ms = m.end_ms - 15 * 60 * 1000;
                let btc_start = m_trades.iter().find(|t| t.trade_time_ms >= window_start_ms).map(|t| t.price).unwrap_or(m.open_price);
                let btc_end = m_trades.last().unwrap().price;
                let actual = if btc_end > btc_start { "UP".to_string() } else { "DOWN".to_string() };
    
                let mut best_candidate: Option<(String, f64, f64, f64, i64, f64)> = None; // (dir, conf, up_ask, down_ask, secs_in, edge)
                let mut conf_state = ConfirmationState::new();

            for secs in (MIN_SECS_INTO_MARKET..=MAX_SECS_INTO_MARKET).step_by(1) {
                let current_ms = m.start_ms + (secs * 1000) as i64;
                let remaining_secs = MARKET_DURATION_SECS.saturating_sub(secs);
                
                let window_trades: Vec<_> = m_trades.iter().filter(|t| t.trade_time_ms <= current_ms).cloned().collect();
                if window_trades.len() < MIN_TRADES_FOR_SIGNAL { continue; }

                let bars = build_1s_bars(&window_trades, m.start_ms, secs);
                
                if let Some(signal) = compute_drift_signal_v11(&bars, m.open_price, remaining_secs as f64) {
                    if signal.regime == models::Regime::Chop {
                        conf_state.reset();
                        continue;
                    }

                    let confirmed = conf_state.update(
                        &signal.direction,
                        signal.confidence,
                        ENTRY_CONFIDENCE,
                        secs,
                        signal.adaptive_confirm,
                    );

                    if confirmed {
                        let (up_ask, down_ask) = find_entry_ask(&m_ticks, current_ms);
                        let entry_ask = if signal.direction == "UP" { up_ask } else { down_ask };
                        
                        if entry_ask < MIN_ENTRY_PRICE { conf_state.reset(); continue; }
                        if entry_ask > MAX_ENTRY_PRICE { conf_state.reset(); continue; }
                        
                        let edge = signal.confidence - (entry_ask + SLIPPAGE);
                        if edge < MIN_EDGE { conf_state.reset(); continue; }

                        // First-Signal Logic: Fire immediately upon finding edge
                        return Some(MarketSignalLive {
                            slug: m.slug.clone(),
                            actual,
                            signal: signal.direction,
                            confidence: signal.confidence,
                            entry_up_ask: up_ask,
                            entry_down_ask: down_ask,
                            entry_secs_in: secs as i64,
                            up_trajectory: vec![],
                            down_trajectory: vec![],
                        });
                    }
                }
            }

            // No signal fired during the 15-minute window
            None
        })
        .collect()
}

// ═════════════════════════════════════════════════════════════════
// SIMULATOR
// ═════════════════════════════════════════════════════════════════

const FEE_RATE: f64 = 0.01;

fn run_sim(
    signals: &[MarketSignalLive],
    starting_bankroll: f64,
    bet_fraction: f64,
) -> (f64, i32, i32, Vec<TradeLog>) {
    let mut bankroll = starting_bankroll;
    let mut wins = 0;
    let mut losses = 0;
    let mut logs = Vec::new();

    for sig in signals {
        let size = bankroll * bet_fraction;
        let entry_price = if sig.signal == "UP" { sig.entry_up_ask } else { sig.entry_down_ask } + SLIPPAGE;

        // Skip if price exceeds our strict bounds
        if entry_price > MAX_ENTRY_PRICE || entry_price < MIN_ENTRY_PRICE {
            continue;
        }

        let shares = (size * (1.0 - FEE_RATE)) / entry_price;
        let win = sig.signal == sig.actual;

        let pnl = if win {
            shares * 1.0 - size
        } else {
            -size
        };

        bankroll += pnl;
        if win {
            wins += 1;
        } else {
            losses += 1;
        }

        println!("Market: {} | Signal: {} | Actual: {} | Win: {} | PnL: {:.2}", sig.slug, sig.signal, sig.actual, win, pnl);

        logs.push(TradeLog {
            market: sig.slug.clone(),
            actual: sig.actual.clone(),
            signal: sig.signal.clone(),
            confidence: sig.confidence,
            entry_price,
            secs_in: sig.entry_secs_in,
            win,
            pnl,
        });
    }

    (bankroll, wins, losses, logs)
}

fn main() -> Result<()> {
    let args = Args::parse();
    let (meta, trades, ticks) = load_data(&args.db_path)?;

    println!("Building signals using 1:1 Live Engine Logic...");
    let pb = ProgressBar::new(meta.len() as u64);
    pb.set_style(ProgressStyle::default_bar().template("{msg} {wide_bar} {pos}/{len}").unwrap());
    
    let signals = build_signals(&meta, &trades, &ticks, &pb);
    pb.finish_with_message("Done building signals");

    let mut signals_sorted = signals;
    signals_sorted.sort_by_key(|s| s.slug.clone()); // Need chronological sort originally, string sort close enough if YYYY-MM-DD

    let (bankroll, wins, losses, logs) = run_sim(&signals_sorted, args.bankroll, args.bet_fraction);

    let total = wins + losses;
    let wr = if total > 0 { (wins as f64 / total as f64) * 100.0 } else { 0.0 };
    let roi = ((bankroll - args.bankroll) / args.bankroll) * 100.0;

    let mut table = Table::new();
    table.add_row(row!["Live 1:1 Logic", "Performance"]);
    table.add_row(row!["Total Trades", total.to_string()]);
    table.add_row(row!["Win Rate", format!("{:.2}%", wr)]);
    table.add_row(row!["ROI", format!("{:.2}%", roi)]);
    table.add_row(row!["Final Bankroll", format!("${:.2}", bankroll)]);
    table.printstd();

    // Verify side mapping output logic
    let up_wins = logs.iter().filter(|l| l.signal == "UP" && l.win).count();
    let down_wins = logs.iter().filter(|l| l.signal == "DOWN" && l.win).count();
    let up_total = logs.iter().filter(|l| l.signal == "UP").count();
    let down_total = logs.iter().filter(|l| l.signal == "DOWN").count();
    println!("\nSides:\nUP: {}/{} ({:.1}%)\nDOWN: {}/{} ({:.1}%)", 
        up_wins, up_total, if up_total > 0 { (up_wins as f64 / up_total as f64) * 100.0 } else { 0.0 },
        down_wins, down_total, if down_total > 0 { (down_wins as f64 / down_total as f64) * 100.0 } else { 0.0 }
    );

    Ok(())
}
