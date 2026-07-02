use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result};
use duckdb::Connection;
use rayon::prelude::*;
use rusqlite::{Connection as SqlConnection, OpenFlags};
use serde::Serialize;

use crate::table_config::{table_by_name, TableConfig, TABLES};

const TS_COLUMNS: &[(&str, &str)] = &[
    ("binance_trades", "trade_time"),
    ("binance_ticks_ms", "source_ts_ms"),
    ("polymarket_ticks_ms", "source_ts_ms"),
    ("lag_pairs_ms", "paired_at_ms"),
];

#[derive(Debug, Clone, Serialize)]
pub struct SqliteFillResult {
    pub table: String,
    pub date: String,
    pub db_path: String,
    pub rows: u64,
    pub status: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SqliteFillReport {
    pub filled: Vec<SqliteFillResult>,
    pub elapsed_seconds: f64,
}

fn day_bounds_ms(date: &str) -> Result<(i64, i64)> {
    use chrono::{NaiveDate, TimeZone, Utc};
    let d = NaiveDate::parse_from_str(date, "%Y-%m-%d").context("parse date")?;
    let start = Utc.from_utc_datetime(&d.and_hms_opt(0, 0, 0).unwrap());
    let end = start + chrono::Duration::days(1);
    Ok((start.timestamp_millis(), end.timestamp_millis()))
}

fn staging_dbs(staging: &Path) -> Result<Vec<PathBuf>> {
    let mut dbs = Vec::new();
    for entry in std::fs::read_dir(staging)? {
        let path = entry?.path();
        let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
        if name.ends_with(".recovered.db") || (name.ends_with(".db") && !name.contains(".recovered.")) {
            if path.is_file() {
                dbs.push(path);
            }
        }
    }
    dbs.sort_by_key(|p| std::cmp::Reverse(p.metadata().map(|m| m.len()).unwrap_or(0)));
    Ok(dbs)
}

fn sqlite_count(db_path: &Path, table: &str, ts_col: &str, lo: i64, hi: i64) -> Result<u64> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let conn = SqlConnection::open_with_flags(db_path, flags)
        .with_context(|| format!("open {}", db_path.display()))?;
    let sql = format!(
        "SELECT COUNT(*) FROM \"{table}\" WHERE \"{ts_col}\" >= ?1 AND \"{ts_col}\" < ?2"
    );
    let count: i64 = conn
        .query_row(&sql, rusqlite::params![lo, hi], |row| row.get(0))
        .unwrap_or(0);
    Ok(count.max(0) as u64)
}

fn best_db(
    staging: &Path,
    table: &str,
    ts_col: &str,
    lo: i64,
    hi: i64,
    dbs: &[PathBuf],
) -> Result<Option<(PathBuf, u64)>> {
    let mut best: Option<(PathBuf, u64)> = None;
    for db in dbs.iter().take(40) {
        let count = sqlite_count(db, table, ts_col, lo, hi).unwrap_or(0);
        if count == 0 {
            continue;
        }
        if best.as_ref().map(|(_, c)| count > *c).unwrap_or(true) {
            best = Some((db.clone(), count));
        }
    }
    Ok(best)
}

fn export_sqlite_date(
    db_path: &Path,
    table: &str,
    ts_col: &str,
    lo: i64,
    hi: i64,
    output_path: &Path,
    compression: &str,
    row_group_size: usize,
) -> Result<u64> {
    let con = Connection::open_in_memory().context("duckdb")?;
    con.execute("INSTALL sqlite; LOAD sqlite;", [])?;
    let attach = format!(
        "ATTACH '{}' AS src (READ_ONLY, TYPE SQLITE)",
        db_path.to_string_lossy()
    );
    con.execute(&attach, [])?;
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = output_path.with_extension("parquet.tmp");
    let sql = format!(
        "COPY (
            SELECT * FROM src.\"{table}\"
            WHERE \"{ts_col}\" >= {lo} AND \"{ts_col}\" < {hi}
            ORDER BY \"{ts_col}\"
        ) TO '{}' (FORMAT PARQUET, COMPRESSION '{compression}', ROW_GROUP_SIZE {row_group_size})",
        tmp.to_string_lossy()
    );
    con.execute(&sql, [])?;
    std::fs::rename(&tmp, output_path)?;
    let count: u64 = con
        .query_row(
            &format!("SELECT COUNT(*)::UBIGINT FROM read_parquet('{}')", output_path.to_string_lossy()),
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    Ok(count)
}

pub fn fill_missing_from_sqlite(
    staging: &Path,
    unified_root: &Path,
    tables: &[&str],
    dates: &[String],
    compression: &str,
    row_group_size: usize,
    parallel_jobs: usize,
) -> Result<SqliteFillReport> {
    let started = Instant::now();
    let dbs = staging_dbs(staging)?;
    let configs: Vec<&TableConfig> = if tables.is_empty() {
        TABLES
            .iter()
            .filter(|t| !t.unpartitioned)
            .filter(|t| TS_COLUMNS.iter().any(|(n, _)| n == &t.name))
            .collect()
    } else {
        tables
            .iter()
            .map(|n| table_by_name(n).with_context(|| format!("unknown table {n}")))
            .collect::<Result<Vec<_>>>()?
    };

    let mut jobs = Vec::new();
    for date in dates {
        let (lo, hi) = day_bounds_ms(date)?;
        for cfg in &configs {
            let ts_col = TS_COLUMNS
                .iter()
                .find(|(n, _)| *n == cfg.name)
                .map(|(_, c)| *c)
                .unwrap_or("");
            let out_dir = unified_root.join(cfg.name).join(format!("date={date}"));
            let out_path = out_dir.join("part-000001.parquet");
            if out_path.exists() {
                continue;
            }
            if let Some((db, rows)) = best_db(staging, cfg.name, ts_col, lo, hi, &dbs)? {
                if rows > 0 {
                    jobs.push((cfg.name.to_string(), date.clone(), db, ts_col.to_string(), out_path, lo, hi));
                }
            }
        }
    }

    eprintln!("sqlite-fill jobs={}", jobs.len());
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(parallel_jobs.max(1))
        .build()
        .context("rayon pool")?;

    let filled = pool.install(|| {
        jobs
            .par_iter()
            .map(|(table, date, db, ts_col, out_path, lo, hi)| {
                let result = export_sqlite_date(
                    db,
                    table,
                    ts_col,
                    *lo,
                    *hi,
                    out_path,
                    compression,
                    row_group_size,
                );
                match result {
                    Ok(rows) => {
                        eprintln!("  {table} {date} rows={rows} db={}", db.file_name().unwrap_or_default().to_string_lossy());
                        SqliteFillResult {
                            table: table.clone(),
                            date: date.clone(),
                            db_path: db.display().to_string(),
                            rows,
                            status: "ok".into(),
                        }
                    }
                    Err(err) => SqliteFillResult {
                        table: table.clone(),
                        date: date.clone(),
                        db_path: db.display().to_string(),
                        rows: 0,
                        status: format!("error: {err}"),
                    },
                }
            })
            .collect::<Vec<_>>()
    });

    Ok(SqliteFillReport {
        filled,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

pub fn missing_partition_dates(unified_root: &Path, table: &str, market_dates: &[String]) -> Vec<String> {
    let table_dir = unified_root.join(table);
    market_dates
        .iter()
        .filter(|date| !table_dir.join(format!("date={date}")).exists())
        .cloned()
        .collect()
}

pub fn market_calendar_dates(unified_root: &Path) -> Result<Vec<String>> {
    use std::collections::BTreeSet;
    let pattern = unified_root.join("market_meta/unpartitioned/*.parquet");
    let mut dates = BTreeSet::new();
    for entry in glob::glob(&pattern.to_string_lossy())? {
        let path = entry?;
        let con = Connection::open_in_memory()?;
        let sql = format!(
            "SELECT market_slug FROM read_parquet('{}') WHERE market_slug LIKE 'btc-updown-15m-%'",
            path.to_string_lossy()
        );
        let mut stmt = con.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let slug: String = row.get(0)?;
            if let Some(epoch_s) = slug.rsplit('-').next().and_then(|s| s.parse::<i64>().ok()) {
                let d = chrono::DateTime::from_timestamp(epoch_s, 0)
                    .map(|dt| dt.format("%Y-%m-%d").to_string());
                if let Some(d) = d {
                    dates.insert(d);
                }
            }
        }
    }
    Ok(dates.into_iter().collect())
}