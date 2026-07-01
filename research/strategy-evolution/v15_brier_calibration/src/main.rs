/// ═══════════════════════════════════════════════════════════════════
/// V11 Production Backtester — Main Entry Point
/// ═══════════════════════════════════════════════════════════════════
///
/// This is the backtester orchestration layer. It:
/// 1. Loads data from SQLite
/// 2. Iterates through market windows in parallel
/// 3. For each window: blacklist check → volume gate → signal scan → trade
/// 4. Computes PnL and saves trade log
///
/// # Module Structure
/// - `config.rs`    → All tunable constants
/// - `types.rs`     → Data structures (Regime, SignalResult, TradeLog)
/// - `blacklist.rs` → Day×hour trading blacklist
/// - `signal.rs`    → Core signal engine (regime, drift, OFI, scoreboard, whipsaw)
/// - `volume.rs`    → Volume gate filter
/// - `main.rs`      → This file (data loading, orchestration, trade simulation)
///
/// # V11 Winning Configuration
/// Based on A/B testing against 60 days of 1-second BTCUSDC data:
/// - **Best Signal Mode**: Wait for peak confidence, don't take first signal
/// - **Volume Gate**: Skip low-volume windows
/// - **Whipsaw Signal**: 4th signal component rewarding moderate chop
/// - Result: **75.4% win rate** (vs 68.9% V10 baseline)

mod config;
mod types;
mod blacklist;
mod signal;
mod volume;
mod calibration;

use anyhow::{Context, Result};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use polars::prelude::*;
use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use std::path::PathBuf;
use prettytable::{Table, row, cell};

use config::*;
use types::*;
use blacklist::is_blacklisted;
use signal::find_best_signal;
use calibration::{BrierCircuitBreaker, CalibrationTable, BRIER_WINDOW_SIZE, BRIER_TRIP_THRESHOLD, CALIBRATION_BIN_WIDTH, CALIBRATION_MIN_SAMPLE};

// ─────────────────────────────────────────────────────────────────
// CLI Arguments
// ─────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(author, version, about = "V15 Brier Calibration Backtester")]
struct Args {
    /// Path to the SQLite database with binance_trades, market_meta, polymarket_ticks_ms
    #[arg(long, default_value = "polymarket_btc_data.db")]
    db_path: PathBuf,

    /// Starting bankroll for simulation
    #[arg(long, default_value_t = DEFAULT_BANKROLL)]
    bankroll: f64,

    /// Fraction of bankroll to risk per trade (Kelly-lite)
    #[arg(long, default_value_t = DEFAULT_BET_FRACTION)]
    bet_fraction: f64,

    /// Minimum signal confidence to consider a trade
    #[arg(long, default_value_t = DEFAULT_MIN_CONFIDENCE)]
    min_confidence: f64,

    /// Minimum edge (confidence - entry_price) required
    #[arg(long, default_value_t = DEFAULT_MIN_EDGE)]
    min_edge: f64,

    /// Minimum entry price (best_ask) to accept
    #[arg(long, default_value_t = DEFAULT_MIN_ENTRY_PRICE)]
    min_entry_price: f64,

    /// Maximum entry price (best_ask) to accept
    #[arg(long, default_value_t = DEFAULT_MAX_ENTRY_PRICE)]
    max_entry_price: f64,

    /// Output CSV path for trade log
    #[arg(long, default_value = "v15_trade_log.csv")]
    output_csv: PathBuf,
}

// ─────────────────────────────────────────────────────────────────
// Data Loading
// ─────────────────────────────────────────────────────────────────

/// Load all data from SQLite into Polars DataFrames.
///
/// Tables required:
/// - `market_meta` (market_slug TEXT, first_seen_ms INTEGER)
/// - `binance_trades` (trade_time INTEGER, price REAL, quantity REAL, is_buyer_maker INTEGER)
/// - `polymarket_ticks_ms` (market_slug TEXT, source_ts_ms INTEGER, side_label TEXT, best_ask REAL, event_type TEXT)
fn load_data(db_path: &PathBuf) -> Result<(DataFrame, DataFrame, DataFrame)> {
    println!("  Connecting to database: {:?}", db_path);
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    conn.execute_batch(
        "PRAGMA query_only = ON;
         PRAGMA mmap_size = 30000000000;
         PRAGMA cache_size = -1048576;
         PRAGMA temp_store = MEMORY;
         PRAGMA synchronous = OFF;",
    )?;

    // ── Market Metadata ──
    println!("  Loading market_meta...");
    let mut stmt = conn.prepare(
        "SELECT market_slug, first_seen_ms FROM market_meta ORDER BY first_seen_ms ASC",
    )?;
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

    // ── Trade Data ──
    println!("  Loading binance_trades...");
    let mut stmt = conn.prepare(
        "SELECT trade_time, price, quantity, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC",
    )?;
    let trades_iter = stmt.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, f64>(1)?,
            row.get::<_, f64>(2)?,
            row.get::<_, i32>(3)?,
        ))
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

    // ── Polymarket Ticks ──
    println!("  Loading polymarket_ticks...");
    let mut stmt = conn.prepare(
        "SELECT market_slug, source_ts_ms, side_label, best_ask FROM polymarket_ticks_ms WHERE event_type = 'price_change' ORDER BY source_ts_ms ASC",
    )?;
    let ticks_iter = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, f64>(3)?,
        ))
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

// ─────────────────────────────────────────────────────────────────
// Market Window Processing
// ─────────────────────────────────────────────────────────────────

/// Build 1-second OHLCV bars from raw trades within a market window.
///
/// # Returns
/// (close_arr, buy_arr, sell_arr) — each of length 900 (15 minutes of 1s bars).
/// Zeros are forward-filled with the last known price.
fn build_1s_bars(
    mkt_trades: &DataFrame,
    start_ms: i64,
    btc_start: f64,
) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
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

        // is_buyer_maker == 0 → taker is buyer (buy volume)
        // is_buyer_maker == 1 → taker is seller (sell volume)
        if makers.get(i).unwrap() == 0 {
            buy_arr[sec] += qtys.get(i).unwrap();
        } else {
            sell_arr[sec] += qtys.get(i).unwrap();
        }
    }

    // Forward-fill: if no trade happened in a second, carry the last price
    let mut cur = btc_start;
    for i in 0..900 {
        if close_arr[i] == 0.0 {
            close_arr[i] = cur;
        } else {
            cur = close_arr[i];
        }
    }

    (close_arr, buy_arr, sell_arr)
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

/// Determine the actual outcome (UP or DOWN) for a market window.
///
/// Uses the first trade after the window ends as the settle price.
fn determine_outcome(
    df_trades: &DataFrame,
    mkt_trades: &DataFrame,
    end_ms: i64,
    btc_start: f64,
) -> (f64, &'static str) {
    let settle_mask = df_trades
        .column("trade_time")
        .unwrap()
        .i64()
        .unwrap()
        .gt_eq(end_ms);
    let settle_trades = df_trades.filter(&settle_mask).unwrap();

    let btc_end = if settle_trades.height() > 0 {
        settle_trades
            .column("price")
            .unwrap()
            .f64()
            .unwrap()
            .get(0)
            .unwrap()
    } else {
        mkt_trades
            .column("price")
            .unwrap()
            .f64()
            .unwrap()
            .get(mkt_trades.height() - 1)
            .unwrap()
    };

    let direction = if btc_end > btc_start { "UP" } else { "DOWN" };
    (btc_end, direction)
}

// ─────────────────────────────────────────────────────────────────
// Factor Regression (Backtesting Significance)
// ─────────────────────────────────────────────────────────────────

/// Compute OLS regression of trade log returns against market return
/// to isolate strategy alpha from pure market exposure, and generate a p-value
fn calculate_ols_alpha_beta_p(returns: &[(f64, f64)]) -> (f64, f64, f64) {
    if returns.len() < 3 {
        return (0.0, 0.0, 1.0);
    }
    let n = returns.len() as f64;
    let sum_x = returns.iter().map(|&(_, x)| x).sum::<f64>();
    let sum_y = returns.iter().map(|&(y, _)| y).sum::<f64>();
    let sum_xx = returns.iter().map(|&(_, x)| x * x).sum::<f64>();
    let sum_xy = returns.iter().map(|&(y, x)| x * y).sum::<f64>();

    let mean_x = sum_x / n;
    let mean_y = sum_y / n;

    let beta = (sum_xy - n * mean_x * mean_y) / (sum_xx - n * mean_x * mean_x + 1e-9);
    let alpha = mean_y - beta * mean_x;

    let mut ss_res = 0.0;
    for &(y, x) in returns {
        let pred = alpha + beta * x;
        ss_res += (y - pred).powi(2);
    }
    let s_err = (ss_res / (n - 2.0)).sqrt();
    let se_alpha = s_err * (1.0 / n + (mean_x * mean_x) / (sum_xx - n * mean_x * mean_x + 1e-9)).sqrt();
    
    let t_stat = alpha / (se_alpha + 1e-9);

    let t_dist = statrs::distribution::StudentsT::new(0.0, 1.0, n - 2.0).unwrap();
    use statrs::distribution::ContinuousCDF;
    // P-value for two-tailed test that alpha != 0
    let p_value = 2.0 * (1.0 - t_dist.cdf(t_stat.abs()));

    (alpha, beta, p_value)
}

fn kelly_bet_fraction(edge: f64, ask_price: f64) -> f64 {
    if edge <= 0.0 || ask_price >= 1.0 || ask_price <= 0.0 {
        return 0.0;
    }
    let odds = 1.0 - ask_price;
    let full_kelly = edge / odds;
    let scaled = full_kelly * KELLY_SCALE;
    scaled.min(MAX_BET_FRACTION).max(0.0)
}

// ─────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────

fn main() -> Result<()> {
    let args = Args::parse();

    println!("============================================================");
    println!(" V15 BRIER CALIBRATION BACKTESTER");
    println!("============================================================");
    println!("  Config: best_signal + volume_gate + whipsaw_signal + brier_cb + calibration");
    println!("  Bet fraction: {:.1}%", args.bet_fraction * 100.0);
    println!("  Min confidence: {:.2}", args.min_confidence);
    println!("  Min edge: {:.2}", args.min_edge);
    println!("  Entry ask bounds: {:.3}–{:.3}", args.min_entry_price, args.max_entry_price);
    println!();

    let (df_meta, df_trades, df_ticks) = load_data(&args.db_path)?;

    let slugs: Vec<String> = df_meta
        .column("market_slug")?
        .str()?
        .into_no_null_iter()
        .map(|s| s.to_string())
        .collect();
    let first_seen_ms: Vec<i64> = df_meta
        .column("first_seen_ms")?
        .i64()?
        .into_no_null_iter()
        .collect();

    // ── Pre-compute volume median for the volume gate ──
    // In the live bot, replace this with a rolling median estimator
    // (see volume::VolumeMedianEstimator).
    let total_vol: f64 = df_trades
        .column("quantity")
        .unwrap()
        .f64()
        .unwrap()
        .into_no_null_iter()
        .sum();
    let total_hours = (slugs.len() as f64 * MARKET_DURATION_SECS as f64 / 3600.0).max(1.0);
    let volume_median = total_vol / total_hours * 0.5;
    println!("  Volume gate median: {:.2} per hour", volume_median);

    // ── Progress Bar ──
    let pb = ProgressBar::new(slugs.len() as u64);
    pb.set_style(
        ProgressStyle::default_bar()
            .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta})")?
            .progress_chars("#>-"),
    );

    // ── Process Markets in Parallel ──
    let results: Vec<Option<TradeLog>> = slugs
        .par_iter()
        .zip(first_seen_ms.par_iter())
        .map(|(slug, &fs_ms): (&String, &i64)| {
            pb.inc(1);

            // ── Step 1: Parse epoch from slug ──
            let epoch_s = slug
                .split('-')
                .last()
                .unwrap_or("0")
                .parse::<i64>()
                .unwrap_or(fs_ms / 1000);
            let start_ms = epoch_s * 1000;

            // ── Step 2: Blacklist Check ──
            // Uses the day-specific (dow, hour_ET) blacklist.
            // This alone lifts win rate from 65.0% → 68.9%.
            if is_blacklisted(epoch_s) {
                return None;
            }

            let end_ms = start_ms + MARKET_DURATION_SECS * 1000;

            // ── Step 3: Filter Trades for This Window ──
            let mask = df_trades.column("trade_time").unwrap().i64().unwrap().gt_eq(start_ms)
                & df_trades.column("trade_time").unwrap().i64().unwrap().lt(end_ms);
            let mkt_trades = df_trades.filter(&mask).ok()?;
            if mkt_trades.height() < 50 {
                return None;
            }

            let btc_start = mkt_trades
                .column("price")
                .unwrap()
                .f64()
                .unwrap()
                .get(0)
                .unwrap();

            // ── Step 4: Volume Gate ──
            // Skip low-volume windows. Adds +1.6% win rate.
            if *ENABLE_VOLUME_GATE {
                let window_vol: f64 = mkt_trades
                    .column("quantity")
                    .unwrap()
                    .f64()
                    .unwrap()
                    .sum()
                    .unwrap_or(0.0);
                let hourly_vol = window_vol / (MARKET_DURATION_SECS as f64 / 3600.0);
                if hourly_vol < volume_median {
                    return None;
                }
            }

            // ── Step 5: Determine Actual Outcome ──
            let (btc_end, actual) = determine_outcome(&df_trades, &mkt_trades, end_ms, btc_start);

            // ── Step 6: Build 1-Second Bars ──
            let (close_arr, buy_arr, sell_arr) = build_1s_bars(&mkt_trades, start_ms, btc_start);

            let tick_mask = df_ticks.column("market_slug").unwrap().str().unwrap().equal(slug.as_str());
            let mkt_ticks = df_ticks.filter(&tick_mask).ok()?;
            let (up_entry_asks, down_entry_asks) = build_entry_ask_ladders(&mkt_ticks, start_ms);

            // ── Step 7: Find Best Signal (core V11 improvement) ──
            // Instead of taking the first qualifying signal, scan the entire
            // entry window and enter at the point of maximum confidence.
            // This single change adds +5.2% win rate.
            let candidate = find_best_signal(
                &close_arr,
                &buy_arr,
                &sell_arr,
                btc_start,
                args.min_confidence,
                args.min_edge,
                args.min_entry_price,
                args.max_entry_price,
                &up_entry_asks,
                &down_entry_asks,
            )?;

            // ── Step 8: Create Trade Record ──
            let final_correct = candidate.direction == actual;

            Some(TradeLog {
                slug: slug.clone(),
                entry_secs_in: candidate.entry_sec,
                side: candidate.direction,
                entry_price: candidate.entry_price,
                exit_price: if final_correct { 1.0 } else { 0.0 },
                pnl: 0.0,
                bankroll: 0.0,
                correct: final_correct,
                conf: candidate.confidence,
                edge: candidate.edge,
                regime: format!("{}", candidate.regime),
                path_eff: candidate.path_eff,
                autocorr: candidate.autocorr,
                consistency: candidate.consistency,
                btc_start,
                btc_end,
            })
        })
        .collect();

    pb.finish_with_message("Backtest complete");

    // ── Compute PnL with Brier Circuit Breaker & Calibration ──
    let mut bankroll = args.bankroll;
    let mut final_trades = Vec::new();
    let mut wins = 0;

    // v15: Sequential calibration monitors
    let mut brier = BrierCircuitBreaker::new(BRIER_WINDOW_SIZE, BRIER_TRIP_THRESHOLD);
    let avg_entry = if results.is_empty() {
        0.50 + SLIPPAGE
    } else {
        results.iter().flatten().map(|t| t.entry_price).sum::<f64>()
            / results.iter().flatten().count() as f64
    };
    let mut cal_table = CalibrationTable::new(CALIBRATION_BIN_WIDTH, CALIBRATION_MIN_SAMPLE, avg_entry);
    let mut skipped_brier = 0usize;
    let mut skipped_bin = 0usize;

    // Sort candidates by time (slug encodes epoch) for sequential processing
    let mut candidates: Vec<TradeLog> = results.into_iter().flatten().collect();
    candidates.sort_by(|a, b| a.slug.cmp(&b.slug));

    for mut t in candidates {
        // ── v15 Gate 1: Brier Circuit Breaker ──
        if brier.is_paused() {
            // Model is miscalibrated — skip this trade but still record outcome
            // for the monitor (so it can detect recovery)
            let outcome = if t.correct { 1.0 } else { 0.0 };
            let recal_conf = cal_table.recalibrated_confidence(t.conf);
            brier.record(recal_conf, outcome);
            cal_table.record(t.conf, t.correct);
            brier.record_skip();
            skipped_brier += 1;
            continue;
        }

        // ── v15 Gate 2: Confidence-bin calibration filter ──
        if !cal_table.is_bin_profitable(t.conf) {
            // This confidence bin has empirically negative edge — skip
            let outcome = if t.correct { 1.0 } else { 0.0 };
            let recal_conf = cal_table.recalibrated_confidence(t.conf);
            brier.record(recal_conf, outcome);
            cal_table.record(t.conf, t.correct);
            skipped_bin += 1;
            continue;
        }

        // ── Execute Trade ──
        let kelly_fraction = kelly_bet_fraction(t.edge, t.entry_price);
        let mut bet_amount = bankroll * kelly_fraction;
        if bet_amount < MIN_BET_SIZE {
            bet_amount = MIN_BET_SIZE;
        }
        if bet_amount > bankroll {
            bet_amount = bankroll;
        }

        let fee_entry = bet_amount * FEE_RATE;
        let shares = (bet_amount - fee_entry) / t.entry_price;

        let payout = shares * t.exit_price;
        let fee_exit = if payout > 0.0 {
            payout * FEE_RATE
        } else {
            0.0
        };

        t.pnl = payout - fee_exit - bet_amount;
        bankroll += t.pnl;
        t.bankroll = bankroll;

        if t.correct {
            wins += 1;
        }

        // Update calibration monitors with this trade's outcome
        // Use recalibrated confidence so Brier tracks relative degradation,
        // not absolute overconfidence
        let outcome = if t.correct { 1.0 } else { 0.0 };
        let recal_conf = cal_table.recalibrated_confidence(t.conf);
        cal_table.record(t.conf, t.correct);
        brier.record(recal_conf, outcome);

        final_trades.push(t);
    }

    // ── Save Trade Log ──
    let csv_path = args.output_csv;
    let mut wtr = csv::Writer::from_path(&csv_path)?;
    for t in &final_trades {
        wtr.serialize(t)?;
    }
    wtr.flush()?;
    println!("  Trade log saved to {}", csv_path.display());

    // ── Print Results ──
    let wr = if final_trades.is_empty() {
        0.0
    } else {
        wins as f64 / final_trades.len() as f64 * 100.0
    };

    let mut table = Table::new();
    table.add_row(row!["Metric", "Value"]);
    table.add_row(row!["Total Trades", final_trades.len()]);
    table.add_row(row!["Win Rate", format!("{:.1}%", wr)]);
    table.add_row(row![
        "Final Bankroll",
        format!("${:.2}", bankroll)
    ]);
    table.add_row(row![
        "Total ROI",
        format!("{:.1}%", (bankroll - args.bankroll) / args.bankroll * 100.0)
    ]);
    
    // ── Statistical Significance Analysis (Factor Regression) ──
    let mut regression_pairs = Vec::new(); // (strategy_return, market_return)
    for t in &final_trades {
        if t.entry_price > 0.0 {
            let bet_amount_implied = 1.0;
            let fee_entry = bet_amount_implied * FEE_RATE;
            let shares = (bet_amount_implied - fee_entry) / t.entry_price;
            let payout = shares * t.exit_price;
            let fee_exit = if payout > 0.0 { payout * FEE_RATE } else { 0.0 };
            let strat_return = (payout - fee_exit) / bet_amount_implied - 1.0;

            let mkt_ret = (t.btc_end - t.btc_start) / t.btc_start;
            
            // Adjust sign if strat predicted DOWN? 
            // The OLS factor regression tests if our edge exists *independent* of market returns
            // So we simply regress strat returns against market returns.
            regression_pairs.push((strat_return, mkt_ret));
        }
    }
    let (alpha_pct, beta, p_value) = calculate_ols_alpha_beta_p(&regression_pairs);

    table.add_row(row![
        "Strategy Alpha (Avg Edge)",
        format!("{:.4}% per trade", alpha_pct * 100.0)
    ]);
    table.add_row(row![
        "Market Beta (Exposure)",
        format!("{:.4}", beta)
    ]);
    let sig_text = if p_value < 0.05 { "(Statistically Significant)" } else { "(NOISY / BS)" };
    table.add_row(row![
        "P-Value (H0: Edge = 0)",
        format!("{:.6} {}", p_value, sig_text)
    ]);
    
    // ── v15: Calibration Report ──
    table.add_row(row!["---", "--- v15 Calibration ---"]);
    table.add_row(row!["Brier Score (final)", format!("{:.4}", brier.current_brier)]);
    table.add_row(row!["Brier Window", format!("{}/{}", brier.window_len(), BRIER_WINDOW_SIZE)]);
    table.add_row(row!["Skipped (Brier CB)", skipped_brier]);
    table.add_row(row!["Skipped (Bin Filter)", skipped_bin]);
    table.add_row(row!["Dynamic Min Conf", format!("{:.2}", cal_table.dynamic_min_confidence(args.min_confidence))]);

    table.printstd();

    // Print calibration bin table
    cal_table.print_table();

    // ── Per-Regime Breakdown ──
    println!("\n  Per-Regime Breakdown:");
    for regime in &["Trend", "Neutral"] {
        let regime_trades: Vec<&TradeLog> = final_trades.iter().filter(|t| t.regime == *regime).collect();
        if regime_trades.is_empty() { continue; }
        let regime_wins = regime_trades.iter().filter(|t| t.correct).count();
        let regime_wr = regime_wins as f64 / regime_trades.len() as f64 * 100.0;
        println!(
            "    {:<10} {:>5} trades, {:.1}% WR, avg conf {:.3}",
            regime,
            regime_trades.len(),
            regime_wr,
            regime_trades.iter().map(|t| t.conf).sum::<f64>() / regime_trades.len() as f64,
        );
    }

    Ok(())
}
