//! Step-3 binary calibration export from unified Parquet (no SQLite).

mod parquet_io;

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use chrono::{TimeZone, Utc};
use csv::Writer;
use rayon::prelude::*;
use serde::Serialize;
use signal_engine::calibrated::{
    build_1s_bars_from_arrays, build_calibrated_feature_snapshot, build_raw_1s_arrays,
    DEFAULT_FEATURE_NAMES,
};
use signal_engine::config::MARKET_DURATION_SECS;
use signal_engine::drift::compute_drift_signal_v14;
use signal_engine::models::{BinanceTrade, MarketInfo};

pub use parquet_io::{
    list_partition_dates, load_day_poly_ticks, load_day_trades, load_market_slugs,
    partition_exists,
};

#[derive(Debug, Clone)]
pub struct Step3Market {
    pub slug: String,
    pub start_ms: i64,
    pub end_ms: i64,
}

#[derive(Debug, Clone)]
pub(crate) struct PolyBookTick {
    ts_ms: i64,
    side: String,
    best_bid: Option<f64>,
    best_ask: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExportSummary {
    pub csv_path: String,
    pub manifest_path: String,
    pub parquet_root: String,
    pub markets_selected: usize,
    pub markets_written: usize,
    pub rows_written: usize,
    pub ties_dropped: usize,
    pub elapsed_seconds: f64,
}

#[derive(Debug, Clone)]
pub struct ExportOptions {
    pub parquet_root: PathBuf,
    pub out_dir: PathBuf,
    pub start_ts_ms: Option<i64>,
    pub end_ts_ms: Option<i64>,
    pub market_limit: Option<usize>,
}

pub fn parse_market_start_ms(slug: &str) -> Option<i64> {
    slug.rsplit('-')
        .next()
        .and_then(|value| value.parse::<i64>().ok())
        .map(|epoch_s| epoch_s * 1000)
}

pub fn list_markets(root: &Path, opts: &ExportOptions) -> Result<Vec<Step3Market>> {
    let slugs = load_market_slugs(root)?;
    let start_bound = opts.start_ts_ms.unwrap_or(i64::MIN);
    let end_bound = opts.end_ts_ms.unwrap_or(i64::MAX);

    let mut markets: Vec<Step3Market> = slugs
        .into_iter()
        .filter_map(|slug| {
            let start_ms = parse_market_start_ms(&slug)?;
            let end_ms = start_ms + (MARKET_DURATION_SECS as i64 * 1000);
            if start_ms >= start_bound && start_ms <= end_bound {
                Some(Step3Market {
                    slug,
                    start_ms,
                    end_ms,
                })
            } else {
                None
            }
        })
        .collect();
    markets.sort_by_key(|m| m.start_ms);

    if let Some(limit) = opts.market_limit {
        if markets.len() > limit {
            markets = markets.split_off(markets.len() - limit);
        }
    }
    Ok(markets)
}

fn utc_date_from_ms(ts_ms: i64) -> String {
    Utc.timestamp_millis_opt(ts_ms)
        .single()
        .map(|dt| dt.format("%Y-%m-%d").to_string())
        .unwrap_or_else(|| "unknown".to_string())
}

fn slice_trades(trades: &[BinanceTrade], start_ms: i64, end_ms: i64) -> &[BinanceTrade] {
    if trades.is_empty() {
        return &[];
    }
    let start_idx = trades.partition_point(|t| t.trade_time_ms < start_ms);
    let end_idx = trades.partition_point(|t| t.trade_time_ms <= end_ms);
    &trades[start_idx..end_idx]
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum MarketSkipReason {
    Written,
    MissingBinancePartition,
    MissingPolyPartition,
    InsufficientTrades,
    TieClose,
    NoPolyTicks,
    NoValidSnapshots,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SnapshotSkipCounts {
    pub insufficient_trades: u32,
    pub incomplete_book: u32,
    pub bars_failed: u32,
    pub drift_failed: u32,
    pub feature_failed: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct MarketAuditRecord {
    pub slug: String,
    pub start_ms: i64,
    pub date: String,
    pub reason: MarketSkipReason,
    pub trade_count: usize,
    pub poly_tick_count: usize,
    pub row_count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snapshot_skips: Option<SnapshotSkipCounts>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DateAuditSummary {
    pub markets_total: usize,
    pub markets_written: usize,
    pub missing_binance_partition: bool,
    pub missing_poly_partition: bool,
    pub reason_counts: HashMap<String, usize>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecoverableSummary {
    pub missing_binance_partition: usize,
    pub missing_poly_partition: usize,
    pub insufficient_trades_sparse: usize,
    pub no_poly_ticks_with_partition: usize,
    pub no_valid_snapshots: usize,
    pub tie_close: usize,
    pub total_recoverable_data_gaps: usize,
    pub total_hard_filters: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuditSummary {
    pub parquet_root: String,
    pub markets_selected: usize,
    pub markets_written: usize,
    pub rows_written: usize,
    pub ties_dropped: usize,
    pub reason_counts: HashMap<String, usize>,
    pub recoverable: RecoverableSummary,
    pub partition_coverage: HashMap<String, PartitionTableCoverage>,
    pub by_date: HashMap<String, DateAuditSummary>,
    pub elapsed_seconds: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct PartitionTableCoverage {
    pub partition_count: usize,
    pub first_date: Option<String>,
    pub last_date: Option<String>,
}

struct MarketExport {
    rows: Vec<Vec<String>>,
    markets_written: usize,
    ties_dropped: usize,
    audit: MarketAuditRecord,
}

fn process_market(
    market: &Step3Market,
    day_trades: &[BinanceTrade],
    poly_ticks: &[PolyBookTick],
    feature_names: &[String],
    has_binance_partition: bool,
    has_poly_partition: bool,
) -> MarketExport {
    let date = utc_date_from_ms(market.start_ms);
    let audit = MarketAuditRecord {
        slug: market.slug.clone(),
        start_ms: market.start_ms,
        date: date.clone(),
        reason: MarketSkipReason::NoValidSnapshots,
        trade_count: 0,
        poly_tick_count: poly_ticks.len(),
        row_count: 0,
        snapshot_skips: None,
    };
    let mut out = MarketExport {
        rows: Vec::new(),
        markets_written: 0,
        ties_dropped: 0,
        audit,
    };

    if !has_binance_partition {
        out.audit.reason = MarketSkipReason::MissingBinancePartition;
        return out;
    }
    if !has_poly_partition {
        out.audit.reason = MarketSkipReason::MissingPolyPartition;
        return out;
    }

    let trades = slice_trades(day_trades, market.start_ms, market.end_ms);
    out.audit.trade_count = trades.len();
    if trades.len() < signal_engine::config::MIN_TRADES_FOR_SIGNAL {
        out.audit.reason = MarketSkipReason::InsufficientTrades;
        return out;
    }

    let market_open_price = trades.first().map(|t| t.price).unwrap_or(0.0);
    let market_close_price = trades.last().map(|t| t.price).unwrap_or(0.0);
    if (market_close_price - market_open_price).abs() <= f64::EPSILON {
        out.ties_dropped = 1;
        out.audit.reason = MarketSkipReason::TieClose;
        return out;
    }
    let label_up_final = i32::from(market_close_price > market_open_price);

    if poly_ticks.is_empty() {
        out.audit.reason = MarketSkipReason::NoPolyTicks;
        return out;
    }

    let mut snapshot_skips = SnapshotSkipCounts::default();

    let (raw_close, raw_buy_vol, raw_sell_vol) =
        build_raw_1s_arrays(trades, market.start_ms, MARKET_DURATION_SECS);

    let mut trade_idx = 0usize;
    let mut poly_idx = 0usize;
    let mut up_best_bid = 0.0;
    let mut up_best_ask = 0.0;
    let mut down_best_bid = 0.0;
    let mut down_best_ask = 0.0;
    let mut market_rows = 0usize;

    for secs_in in (15_u64..MARKET_DURATION_SECS).step_by(5) {
        let row_ts_ms = market.start_ms + (secs_in as i64 * 1000);
        while trade_idx < trades.len() && trades[trade_idx].trade_time_ms <= row_ts_ms {
            trade_idx += 1;
        }
        while poly_idx < poly_ticks.len() && poly_ticks[poly_idx].ts_ms <= row_ts_ms {
            let tick = &poly_ticks[poly_idx];
            match tick.side.as_str() {
                "UP" => {
                    if let Some(best_bid) = tick.best_bid {
                        up_best_bid = best_bid;
                    }
                    if let Some(best_ask) = tick.best_ask {
                        up_best_ask = best_ask;
                    }
                }
                "DOWN" => {
                    if let Some(best_bid) = tick.best_bid {
                        down_best_bid = best_bid;
                    }
                    if let Some(best_ask) = tick.best_ask {
                        down_best_ask = best_ask;
                    }
                }
                _ => {}
            }
            poly_idx += 1;
        }

        if trade_idx < signal_engine::config::MIN_TRADES_FOR_SIGNAL {
            snapshot_skips.insufficient_trades += 1;
            continue;
        }
        if up_best_bid <= 0.0
            || up_best_ask <= 0.0
            || down_best_bid <= 0.0
            || down_best_ask <= 0.0
        {
            snapshot_skips.incomplete_book += 1;
            continue;
        }

        let Some(bars) =
            build_1s_bars_from_arrays(&raw_close, &raw_buy_vol, &raw_sell_vol, secs_in)
        else {
            snapshot_skips.bars_failed += 1;
            continue;
        };
        let secs_left = MARKET_DURATION_SECS - secs_in;
        let Some(signal) = compute_drift_signal_v14(&bars, market_open_price, secs_left as f64)
        else {
            snapshot_skips.drift_failed += 1;
            continue;
        };

        let market_state = MarketInfo {
            slug: market.slug.clone(),
            start_ms: market.start_ms,
            end_ms: market.end_ms,
            up_price: (up_best_bid + up_best_ask) / 2.0,
            down_price: (down_best_bid + down_best_ask) / 2.0,
            up_best_ask,
            down_best_ask,
            up_best_bid,
            down_best_bid,
        };
        let snapshot = build_calibrated_feature_snapshot(
            &bars,
            market_open_price,
            &market_state,
            secs_in,
            &signal,
            trade_idx,
        );
        let Ok(feature_values) = snapshot.ordered_values(feature_names) else {
            snapshot_skips.feature_failed += 1;
            continue;
        };

        let mut record = vec![
            market.slug.clone(),
            market.start_ms.to_string(),
            market.end_ms.to_string(),
            row_ts_ms.to_string(),
            format!("{:.8}", market_open_price),
            format!("{:.8}", market_close_price),
            label_up_final.to_string(),
        ];
        record.extend(feature_values.into_iter().map(|v| format!("{:.8}", v)));
        out.rows.push(record);
        market_rows += 1;
    }

    if market_rows > 0 {
        out.markets_written = 1;
        out.audit.reason = MarketSkipReason::Written;
        out.audit.row_count = market_rows;
    } else {
        out.audit.reason = MarketSkipReason::NoValidSnapshots;
        out.audit.snapshot_skips = Some(snapshot_skips);
    }
    out
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

pub fn export_step3(opts: ExportOptions) -> Result<ExportSummary> {
    let started = std::time::Instant::now();
    fs::create_dir_all(&opts.out_dir)?;

    let markets = list_markets(&opts.parquet_root, &opts)?;
    let markets_selected = markets.len();
    let feature_names: Vec<String> = DEFAULT_FEATURE_NAMES
        .iter()
        .map(|s| (*s).to_string())
        .collect();

    let mut by_date: HashMap<String, Vec<Step3Market>> = HashMap::new();
    for market in markets {
        by_date
            .entry(utc_date_from_ms(market.start_ms))
            .or_default()
            .push(market);
    }

    let mut all_rows: Vec<Vec<String>> = Vec::new();
    let mut markets_written = 0usize;
    let mut ties_dropped = 0usize;

    let mut dates: Vec<String> = by_date.keys().cloned().collect();
    dates.sort();

    for date in &dates {
        let day_markets = by_date.get(date).cloned().unwrap_or_default();
        if day_markets.is_empty() {
            continue;
        }
        eprintln!(
            "processing date={date} markets={} …",
            day_markets.len()
        );

        let min_ts = day_markets.iter().map(|m| m.start_ms).min().unwrap_or(0);
        let max_ts = day_markets
            .iter()
            .map(|m| m.end_ms)
            .max()
            .unwrap_or(0);
        let slug_set: HashSet<String> = day_markets.iter().map(|m| m.slug.clone()).collect();

        let has_binance = partition_exists(&opts.parquet_root, "binance_trades", date);
        let has_poly = partition_exists(&opts.parquet_root, "polymarket_ticks_ms", date);
        let day_trades = load_day_trades(&opts.parquet_root, date, min_ts, max_ts)?;
        let poly_by_slug =
            load_day_poly_ticks(&opts.parquet_root, date, &slug_set, min_ts, max_ts)?;

        let day_results: Vec<MarketExport> = day_markets
            .par_iter()
            .map(|market| {
                let ticks = poly_by_slug
                    .get(&market.slug)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                process_market(
                    market,
                    &day_trades,
                    ticks,
                    &feature_names,
                    has_binance,
                    has_poly,
                )
            })
            .collect();

        let day_rows: usize = day_results.iter().map(|r| r.rows.len()).sum();
        let day_written: usize = day_results.iter().map(|r| r.markets_written).sum();
        eprintln!("  date={date} wrote_markets={day_written} rows={day_rows}");

        for result in day_results {
            markets_written += result.markets_written;
            ties_dropped += result.ties_dropped;
            all_rows.extend(result.rows);
        }
    }

    let ts = now_ms();
    let csv_path = opts
        .out_dir
        .join(format!("step3_binary_calibration_{ts}.csv"));
    let manifest_path = opts
        .out_dir
        .join(format!("step3_binary_calibration_{ts}.manifest.json"));

    let mut header: Vec<String> = vec![
        "market_slug".into(),
        "market_start_ms".into(),
        "market_end_ms".into(),
        "ts_ms".into(),
        "market_open_price".into(),
        "market_close_price".into(),
        "label_up_final".into(),
    ];
    header.extend(feature_names.clone());

    let mut writer = Writer::from_path(&csv_path)?;
    writer.write_record(&header)?;
    for row in &all_rows {
        writer.write_record(row)?;
    }
    writer.flush()?;

    let rows_written = all_rows.len();
    let manifest = serde_json::json!({
        "generated_at_ms": ts,
        "csv_path": csv_path.to_string_lossy(),
        "parquet_root": opts.parquet_root.to_string_lossy(),
        "source": "export_step3_from_parquet",
        "start_ts_ms": opts.start_ts_ms,
        "end_ts_ms": opts.end_ts_ms,
        "market_limit": opts.market_limit,
        "feature_names": feature_names,
        "markets_selected": markets_selected,
        "markets_written": markets_written,
        "rows_written": rows_written,
        "ties_dropped": ties_dropped,
        "elapsed_seconds": started.elapsed().as_secs_f64(),
    });

    let mut file = File::create(&manifest_path)?;
    write!(file, "{}", serde_json::to_string_pretty(&manifest)?)?;

    Ok(ExportSummary {
        csv_path: csv_path.to_string_lossy().into_owned(),
        manifest_path: manifest_path.to_string_lossy().into_owned(),
        parquet_root: opts.parquet_root.to_string_lossy().into_owned(),
        markets_selected,
        markets_written,
        rows_written,
        ties_dropped,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

fn reason_key(reason: MarketSkipReason) -> &'static str {
    match reason {
        MarketSkipReason::Written => "written",
        MarketSkipReason::MissingBinancePartition => "missing_binance_partition",
        MarketSkipReason::MissingPolyPartition => "missing_poly_partition",
        MarketSkipReason::InsufficientTrades => "insufficient_trades",
        MarketSkipReason::TieClose => "tie_close",
        MarketSkipReason::NoPolyTicks => "no_poly_ticks",
        MarketSkipReason::NoValidSnapshots => "no_valid_snapshots",
    }
}

fn bump_reason(counts: &mut HashMap<String, usize>, reason: MarketSkipReason) {
    *counts.entry(reason_key(reason).to_string()).or_default() += 1;
}

pub fn audit_step3(opts: ExportOptions) -> Result<AuditSummary> {
    let started = std::time::Instant::now();
    let markets = list_markets(&opts.parquet_root, &opts)?;
    let markets_selected = markets.len();
    let feature_names: Vec<String> = DEFAULT_FEATURE_NAMES
        .iter()
        .map(|s| (*s).to_string())
        .collect();

    let mut by_date: HashMap<String, Vec<Step3Market>> = HashMap::new();
    for market in markets {
        by_date
            .entry(utc_date_from_ms(market.start_ms))
            .or_default()
            .push(market);
    }

    let mut reason_counts: HashMap<String, usize> = HashMap::new();
    let mut by_date_summary: HashMap<String, DateAuditSummary> = HashMap::new();
    let mut recoverable = RecoverableSummary {
        missing_binance_partition: 0,
        missing_poly_partition: 0,
        insufficient_trades_sparse: 0,
        no_poly_ticks_with_partition: 0,
        no_valid_snapshots: 0,
        tie_close: 0,
        total_recoverable_data_gaps: 0,
        total_hard_filters: 0,
    };

    let mut markets_written = 0usize;
    let mut rows_written = 0usize;
    let mut ties_dropped = 0usize;

    let mut dates: Vec<String> = by_date.keys().cloned().collect();
    dates.sort();

    for date in &dates {
        let day_markets = by_date.get(date).cloned().unwrap_or_default();
        if day_markets.is_empty() {
            continue;
        }
        eprintln!("auditing date={date} markets={} …", day_markets.len());

        let min_ts = day_markets.iter().map(|m| m.start_ms).min().unwrap_or(0);
        let max_ts = day_markets
            .iter()
            .map(|m| m.end_ms)
            .max()
            .unwrap_or(0);
        let slug_set: HashSet<String> = day_markets.iter().map(|m| m.slug.clone()).collect();

        let has_binance = partition_exists(&opts.parquet_root, "binance_trades", date);
        let has_poly = partition_exists(&opts.parquet_root, "polymarket_ticks_ms", date);
        let day_trades = load_day_trades(&opts.parquet_root, date, min_ts, max_ts)?;
        let poly_by_slug =
            load_day_poly_ticks(&opts.parquet_root, date, &slug_set, min_ts, max_ts)?;

        let day_results: Vec<MarketExport> = day_markets
            .par_iter()
            .map(|market| {
                let ticks = poly_by_slug
                    .get(&market.slug)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                process_market(
                    market,
                    &day_trades,
                    ticks,
                    &feature_names,
                    has_binance,
                    has_poly,
                )
            })
            .collect();

        let mut date_reason_counts: HashMap<String, usize> = HashMap::new();
        let mut date_written = 0usize;
        for result in &day_results {
            bump_reason(&mut reason_counts, result.audit.reason);
            bump_reason(&mut date_reason_counts, result.audit.reason);
            markets_written += result.markets_written;
            rows_written += result.rows.len();
            ties_dropped += result.ties_dropped;
            if result.markets_written > 0 {
                date_written += 1;
            }

            match result.audit.reason {
                MarketSkipReason::MissingBinancePartition => {
                    recoverable.missing_binance_partition += 1;
                }
                MarketSkipReason::MissingPolyPartition => {
                    recoverable.missing_poly_partition += 1;
                }
                MarketSkipReason::InsufficientTrades => {
                    recoverable.insufficient_trades_sparse += 1;
                }
                MarketSkipReason::NoPolyTicks => {
                    recoverable.no_poly_ticks_with_partition += 1;
                }
                MarketSkipReason::NoValidSnapshots => {
                    recoverable.no_valid_snapshots += 1;
                }
                MarketSkipReason::TieClose => {
                    recoverable.tie_close += 1;
                }
                MarketSkipReason::Written => {}
            }
        }

        recoverable.total_recoverable_data_gaps = recoverable.missing_binance_partition
            + recoverable.missing_poly_partition
            + recoverable.insufficient_trades_sparse
            + recoverable.no_poly_ticks_with_partition
            + recoverable.no_valid_snapshots;
        recoverable.total_hard_filters = recoverable.tie_close;

        by_date_summary.insert(
            date.clone(),
            DateAuditSummary {
                markets_total: day_markets.len(),
                markets_written: date_written,
                missing_binance_partition: !has_binance,
                missing_poly_partition: !has_poly,
                reason_counts: date_reason_counts,
            },
        );
    }

    let mut partition_coverage = HashMap::new();
    for table in ["binance_trades", "polymarket_ticks_ms", "market_meta"] {
        let dates = list_partition_dates(&opts.parquet_root, table)?;
        partition_coverage.insert(
            table.to_string(),
            PartitionTableCoverage {
                partition_count: dates.len(),
                first_date: dates.first().cloned(),
                last_date: dates.last().cloned(),
            },
        );
    }

    Ok(AuditSummary {
        parquet_root: opts.parquet_root.to_string_lossy().into_owned(),
        markets_selected,
        markets_written,
        rows_written,
        ties_dropped,
        reason_counts,
        recoverable,
        partition_coverage,
        by_date: by_date_summary,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}