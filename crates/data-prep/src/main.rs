use anyhow::{Context, Result};
use chrono::{Duration, NaiveDate, NaiveDateTime, Utc};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use rusqlite::{params, Connection};
use std::path::{Path, PathBuf};

/// The Binance 1s kline CSV columns (no header):
///   open_time, open, high, low, close, volume, close_time,
///   quote_volume, num_trades, taker_buy_base_vol, taker_buy_quote_vol, ignore
///
/// Timestamps are in microseconds.

const MARKET_DURATION_SECS: i64 = 900; // 15-minute windows

#[derive(Parser, Debug)]
#[command(name = "data-prep")]
#[command(about = "Convert Binance 1s kline CSVs into the backtest SQLite DB")]
struct Args {
    /// Directory containing raw CSV files
    #[arg(short, long, default_value = "data/raw")]
    input: PathBuf,

    /// Output SQLite database path
    #[arg(short, long, default_value = "data/backtest.db")]
    output: PathBuf,

    /// Market window duration in seconds (default 15 min)
    #[arg(long, default_value_t = 900)]
    window_secs: i64,

    /// Step between market windows in seconds (default = window_secs, non-overlapping)
    #[arg(long)]
    step_secs: Option<i64>,
}

#[derive(Debug)]
struct KlineRow {
    open_time_us: i64,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
    close_time_us: i64,
    _quote_volume: f64,
    num_trades: i64,
    taker_buy_base_vol: f64,
    _taker_buy_quote_vol: f64,
}

fn parse_csv_file(path: &Path) -> Result<Vec<KlineRow>> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .from_path(path)
        .with_context(|| format!("Failed to open {}", path.display()))?;

    let mut rows = Vec::new();

    for result in reader.records() {
        let record = result?;
        if record.len() < 12 {
            continue;
        }

        rows.push(KlineRow {
            open_time_us: record[0].parse()?,
            open: record[1].parse()?,
            high: record[2].parse()?,
            low: record[3].parse()?,
            close: record[4].parse()?,
            volume: record[5].parse()?,
            close_time_us: record[6].parse()?,
            _quote_volume: record[7].parse()?,
            num_trades: record[8].parse()?,
            taker_buy_base_vol: record[9].parse()?,
            _taker_buy_quote_vol: record[10].parse()?,
        });
    }

    Ok(rows)
}

fn create_tables(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS binance_trades (
            trade_time   INTEGER NOT NULL,
            price        REAL    NOT NULL,
            quantity     REAL    NOT NULL,
            is_buyer_maker INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_meta (
            market_slug    TEXT    PRIMARY KEY,
            first_seen_ms  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS polymarket_ticks_ms (
            market_slug   TEXT    NOT NULL,
            source_ts_ms  INTEGER NOT NULL,
            side_label    TEXT    NOT NULL,
            best_ask      REAL    NOT NULL,
            event_type    TEXT    NOT NULL DEFAULT 'price_change'
        );

        CREATE INDEX IF NOT EXISTS idx_trades_time ON binance_trades(trade_time);
        CREATE INDEX IF NOT EXISTS idx_ticks_slug  ON polymarket_ticks_ms(market_slug, source_ts_ms);
        "
    )?;
    Ok(())
}

fn main() -> Result<()> {
    let args = Args::parse();
    let step_secs = args.step_secs.unwrap_or(args.window_secs);

    println!("============================================================");
    println!(" DATA PREP: Binance 1s klines → Backtest DB");
    println!("============================================================");
    println!("  Input:       {}", args.input.display());
    println!("  Output:      {}", args.output.display());
    println!(
        "  Window:      {}s ({}min)",
        args.window_secs,
        args.window_secs / 60
    );
    println!("  Step:        {}s", step_secs);
    println!();

    // ── 1. Discover and sort CSV files ─────────────────────────────
    let mut csv_files: Vec<PathBuf> = std::fs::read_dir(&args.input)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map_or(false, |e| e == "csv"))
        .collect();

    csv_files.sort();
    println!("📂 Found {} CSV files", csv_files.len());

    if csv_files.is_empty() {
        anyhow::bail!("No CSV files found in {}", args.input.display());
    }

    // ── 2. Create DB ───────────────────────────────────────────────
    if let Some(parent) = args.output.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // Remove existing DB for a clean build
    if args.output.exists() {
        std::fs::remove_file(&args.output)?;
    }

    let conn = Connection::open(&args.output)?;
    conn.execute_batch(
        "PRAGMA journal_mode = WAL; PRAGMA synchronous = OFF; PRAGMA cache_size = -1048576;",
    )?;
    create_tables(&conn)?;

    // ── 3. Load all klines into binance_trades ─────────────────────
    // The v10 strategy expects raw trades with:
    //   trade_time (ms), price, quantity, is_buyer_maker
    //
    // From 1s klines we synthesize two "trades" per bar:
    //   - A buyer trade:  quantity = taker_buy_base_vol, is_buyer_maker = 0
    //   - A seller trade: quantity = volume - taker_buy_base_vol, is_buyer_maker = 1
    // Both get the close price and the bar's open_time (converted to ms).

    println!("\n📥 Ingesting klines as synthetic trades...");

    let pb = ProgressBar::new(csv_files.len() as u64);
    pb.set_style(
        ProgressStyle::default_bar()
            .template(
                "{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta})",
            )?
            .progress_chars("#>-"),
    );

    let mut total_rows: u64 = 0;

    conn.execute_batch("BEGIN TRANSACTION;")?;
    {
        let mut stmt = conn.prepare(
            "INSERT INTO binance_trades (trade_time, price, quantity, is_buyer_maker) VALUES (?1, ?2, ?3, ?4)"
        )?;

        for csv_path in &csv_files {
            let klines = parse_csv_file(csv_path)?;

            for k in &klines {
                // Convert microseconds → milliseconds for the strategy
                let time_ms = k.open_time_us / 1000;

                let buy_vol = k.taker_buy_base_vol;
                let sell_vol = k.volume - k.taker_buy_base_vol;

                // Insert buyer trade
                if buy_vol > 0.0 {
                    stmt.execute(params![time_ms, k.close, buy_vol, 0])?;
                    total_rows += 1;
                }
                // Insert seller trade
                if sell_vol > 0.0 {
                    stmt.execute(params![time_ms, k.close, sell_vol, 1])?;
                    total_rows += 1;
                }
            }

            pb.inc(1);
        }
    }
    conn.execute_batch("COMMIT;")?;
    pb.finish_with_message("Trades loaded");

    println!("  ✅ {} synthetic trades inserted", total_rows);

    // ── 4. Generate market windows ─────────────────────────────────
    // Find the time range from the trades, then create non-overlapping
    // (or overlapping, based on step_secs) windows of window_secs length.
    // Each window becomes a "market" with a slug like "btcusdc-{epoch_s}".

    println!(
        "\n📊 Generating {}-second market windows...",
        args.window_secs
    );

    let (min_ms, max_ms): (i64, i64) = {
        let mut stmt =
            conn.prepare("SELECT MIN(trade_time), MAX(trade_time) FROM binance_trades")?;
        stmt.query_row([], |row| Ok((row.get(0)?, row.get(1)?)))?
    };

    println!(
        "  Data range: {} → {}",
        chrono::DateTime::from_timestamp_millis(min_ms).unwrap(),
        chrono::DateTime::from_timestamp_millis(max_ms).unwrap(),
    );

    let mut market_count: u64 = 0;
    conn.execute_batch("BEGIN TRANSACTION;")?;
    {
        let mut stmt =
            conn.prepare("INSERT INTO market_meta (market_slug, first_seen_ms) VALUES (?1, ?2)")?;

        let mut window_start_ms = min_ms;
        let step_ms = step_secs * 1000;

        while window_start_ms + (args.window_secs * 1000) <= max_ms {
            let epoch_s = window_start_ms / 1000;
            let slug = format!("btcusdc-{}", epoch_s);

            stmt.execute(params![slug, window_start_ms])?;
            market_count += 1;

            window_start_ms += step_ms;
        }
    }
    conn.execute_batch("COMMIT;")?;

    println!("  ✅ {} market windows created", market_count);

    // ── 5. Generate synthetic polymarket ticks ──────────────────────
    // The strategy uses poly ticks for entry pricing. Since we don't have
    // Polymarket data, we create synthetic ticks where the "best_ask"
    // starts at 0.50 (50/50 odds) for both UP and DOWN sides,
    // effectively making the strategy rely purely on its signal confidence
    // vs a fair-odds baseline.

    println!("\n🎯 Generating synthetic market ticks (fair-odds baseline)...");

    conn.execute_batch("BEGIN TRANSACTION;")?;
    {
        let mut meta_stmt = conn.prepare("SELECT market_slug, first_seen_ms FROM market_meta")?;
        let mut tick_stmt = conn.prepare(
            "INSERT INTO polymarket_ticks_ms (market_slug, source_ts_ms, side_label, best_ask, event_type) VALUES (?1, ?2, ?3, ?4, 'price_change')"
        )?;

        let markets: Vec<(String, i64)> = meta_stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })?
            .filter_map(|r| r.ok())
            .collect();

        for (slug, first_seen_ms) in &markets {
            // Place a tick at the start of the window for both sides
            tick_stmt.execute(params![slug, first_seen_ms, "UP", 0.50])?;
            tick_stmt.execute(params![slug, first_seen_ms, "DOWN", 0.50])?;
        }
    }
    conn.execute_batch("COMMIT;")?;

    println!("  ✅ Synthetic ticks created (0.50 ask for UP/DOWN)");

    // ── 6. Summary ─────────────────────────────────────────────────
    let db_size = std::fs::metadata(&args.output)?.len();
    println!("\n============================================================");
    println!(" ✅ DONE");
    println!("============================================================");
    println!("  Database:    {}", args.output.display());
    println!("  Size:        {:.1} MB", db_size as f64 / 1_048_576.0);
    println!("  Trades:      {}", total_rows);
    println!("  Markets:     {}", market_count);
    println!("  Window:      {}s", args.window_secs);
    println!();
    println!("  Run the backtester:");
    println!("    cd strategies/v10_0_polars_performance");
    println!(
        "    cargo run --release -- --db-path ../../{}",
        args.output.display()
    );

    Ok(())
}
