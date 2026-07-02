use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use glob::glob;
use polars::prelude::*;
use signal_engine::models::BinanceTrade;

use crate::PolyBookTick;

pub fn partition_dir(root: &Path, table: &str, date: &str) -> PathBuf {
    root.join(table).join(format!("date={date}"))
}

pub fn list_partition_dates(root: &Path, table: &str) -> Result<Vec<String>> {
    let dir = root.join(table);
    if !dir.exists() {
        return Ok(Vec::new());
    }
    let mut dates = Vec::new();
    for entry in std::fs::read_dir(&dir)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if let Some(date) = name.strip_prefix("date=") {
            dates.push(date.to_string());
        }
    }
    dates.sort();
    Ok(dates)
}

pub fn partition_exists(root: &Path, table: &str, date: &str) -> bool {
    let dir = partition_dir(root, table, date);
    dir.exists() && dir.read_dir().map(|mut d| d.next().is_some()).unwrap_or(false)
}

fn parquet_glob(root: &Path, table: &str, date: &str) -> Result<String> {
    Ok(partition_dir(root, table, date)
        .join("*.parquet")
        .to_string_lossy()
        .into_owned())
}

fn scan_day(root: &Path, table: &str, date: &str) -> Result<DataFrame> {
    let pattern = parquet_glob(root, table, date)?;
    if glob(&pattern)?.next().is_none() {
        return Ok(DataFrame::empty());
    }
    LazyFrame::scan_parquet(&pattern, ScanArgsParquet::default())?
        .collect()
        .with_context(|| format!("scan {table} date={date}"))
}

pub fn load_day_trades(root: &Path, date: &str, min_ts: i64, max_ts: i64) -> Result<Vec<BinanceTrade>> {
    if !partition_exists(root, "binance_trades", date) {
        return Ok(Vec::new());
    }
    let df = scan_day(root, "binance_trades", date)?;
    if df.height() == 0 {
        return Ok(Vec::new());
    }

    let trade_time = df.column("trade_time")?.cast(&DataType::Int64)?;
    let trade_time = trade_time.i64()?;
    let price = df.column("price")?.f64()?;
    let quantity = df.column("quantity")?.f64()?;
    let is_buyer_maker = df.column("is_buyer_maker")?;

    let mut out = Vec::new();
    for i in 0..df.height() {
        let ts = trade_time.get(i).unwrap_or(0);
        if ts < min_ts || ts > max_ts {
            continue;
        }
        let maker = match is_buyer_maker.get(i) {
            Ok(AnyValue::Boolean(v)) => v,
            Ok(AnyValue::Int64(v)) => v != 0,
            Ok(AnyValue::UInt64(v)) => v != 0,
            Ok(AnyValue::Int32(v)) => v != 0,
            Ok(AnyValue::UInt8(v)) => v != 0,
            _ => false,
        };
        out.push(BinanceTrade {
            trade_time_ms: ts,
            price: price.get(i).unwrap_or(0.0),
            quantity: quantity.get(i).unwrap_or(0.0),
            is_buyer_maker: maker,
        });
    }
    out.sort_by_key(|t| t.trade_time_ms);
    Ok(out)
}

pub fn load_day_poly_ticks(
    root: &Path,
    date: &str,
    slug_set: &HashSet<String>,
    min_ts: i64,
    max_ts: i64,
) -> Result<HashMap<String, Vec<PolyBookTick>>> {
    let mut grouped: HashMap<String, Vec<PolyBookTick>> = HashMap::new();
    if slug_set.is_empty() || !partition_exists(root, "polymarket_ticks_ms", date) {
        return Ok(grouped);
    }

    let df = scan_day(root, "polymarket_ticks_ms", date)?;
    if df.height() == 0 {
        return Ok(grouped);
    }

    let ts = df.column("source_ts_ms")?.cast(&DataType::Int64)?;
    let ts = ts.i64()?;
    let slug_col = df.column("market_slug")?.str()?;
    let side_col = df.column("side_label")?.str()?;
    let bid_col = df.column("best_bid")?.f64()?;
    let ask_col = df.column("best_ask")?.f64()?;

    for i in 0..df.height() {
        let Some(slug) = slug_col.get(i) else {
            continue;
        };
        if !slug_set.contains(slug) {
            continue;
        }
        let ts_ms = ts.get(i).unwrap_or(0);
        if ts_ms < min_ts || ts_ms > max_ts {
            continue;
        }
        let Some(side) = side_col.get(i) else {
            continue;
        };
        if side != "UP" && side != "DOWN" {
            continue;
        }
        grouped
            .entry(slug.to_string())
            .or_default()
            .push(PolyBookTick {
                ts_ms,
                side: side.to_string(),
                best_bid: bid_col.get(i),
                best_ask: ask_col.get(i),
            });
    }

    for ticks in grouped.values_mut() {
        ticks.sort_by_key(|t| t.ts_ms);
    }
    Ok(grouped)
}

pub fn load_market_slugs(root: &Path) -> Result<Vec<String>> {
    let pattern = root
        .join("market_meta/unpartitioned/*.parquet")
        .to_string_lossy()
        .into_owned();
    let mut slugs = Vec::new();
    for entry in glob(&pattern).context("glob market_meta")? {
        let path = entry?.to_string_lossy().into_owned();
        let df = LazyFrame::scan_parquet(&path, ScanArgsParquet::default())?
            .collect()
            .context("read market_meta")?;
        let col = df.column("market_slug")?.str()?;
        for i in 0..df.height() {
            if let Some(slug) = col.get(i) {
                if slug.starts_with("btc-updown-15m-") {
                    slugs.push(slug.to_string());
                }
            }
        }
    }
    slugs.sort();
    slugs.dedup();
    Ok(slugs)
}