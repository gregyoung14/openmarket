mod sqlite_fill;
mod table_config;

pub use sqlite_fill::{
    fill_missing_from_sqlite, market_calendar_dates, missing_partition_dates, SqliteFillReport,
};

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{bail, Context, Result};
use duckdb::Connection;
use rayon::prelude::*;
use serde::Serialize;
use table_config::{table_by_name, TableConfig, TABLES};

pub use table_config::TABLES as ALL_TABLES;

#[derive(Debug, Clone, Serialize)]
pub struct DateDelta {
    pub date: String,
    pub full_rows: u64,
    pub unified_rows: u64,
    pub delta_rows: i64,
    pub full_shards: usize,
    pub unified_shards: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct TableScan {
    pub table: String,
    pub dates_with_delta: usize,
    pub total_delta_rows: i64,
    pub dates: Vec<DateDelta>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScanReport {
    pub full_root: String,
    pub unified_root: String,
    pub tables: Vec<TableScan>,
    pub repairable_dates: BTreeSet<String>,
    pub elapsed_seconds: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct RepairResult {
    pub table: String,
    pub date: String,
    pub input_rows: u64,
    pub output_rows: u64,
    pub input_shards: usize,
    pub status: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct RepairReport {
    pub repaired: Vec<RepairResult>,
    pub elapsed_seconds: f64,
}

fn quote_ident(name: &str) -> String {
    format!("\"{name}\"")
}

fn parquet_files(dir: &Path) -> Result<Vec<PathBuf>> {
    if !dir.exists() {
        return Ok(Vec::new());
    }
    let mut files: Vec<PathBuf> = glob::glob(&dir.join("*.parquet").to_string_lossy())?
        .filter_map(|e| e.ok())
        .collect();
    files.sort();
    Ok(files)
}

fn parquet_source(files: &[PathBuf]) -> String {
    let quoted: Vec<String> = files
        .iter()
        .map(|p| format!("'{}'", p.to_string_lossy()))
        .collect();
    format!("read_parquet([{}], union_by_name=true)", quoted.join(", "))
}

fn count_rows(con: &Connection, source: &str) -> Result<u64> {
    let sql = format!("SELECT COUNT(*)::UBIGINT FROM {source}");
    let mut stmt = con.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    let row = rows.next()?.context("count row")?;
    Ok(row.get(0)?)
}

fn dedupe_sql(source: &str, cfg: &TableConfig) -> String {
    let partition_expr = cfg
        .dedupe_cols
        .iter()
        .map(|c| quote_ident(c))
        .collect::<Vec<_>>()
        .join(", ");
    let order_expr = cfg
        .order_cols
        .iter()
        .map(|c| format!("{} DESC NULLS LAST", quote_ident(c)))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "SELECT * EXCLUDE (rn) FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY {partition_expr}
                       ORDER BY {order_expr}
                   ) AS rn
            FROM {source}
        ) WHERE rn = 1"
    )
}

pub fn scan_partitions(full_root: &Path, unified_root: &Path, tables: &[&str]) -> Result<ScanReport> {
    let started = Instant::now();
    let con = Connection::open_in_memory().context("duckdb")?;
    let _ = con.execute("PRAGMA threads=4", []);

    let configs: Vec<&TableConfig> = if tables.is_empty() {
        TABLES.iter().collect()
    } else {
        tables
            .iter()
            .map(|name| table_by_name(name).with_context(|| format!("unknown table {name}")))
            .collect::<Result<Vec<_>>>()?
    };

    let mut report_tables = Vec::new();
    let mut repairable_dates = BTreeSet::new();

    for cfg in configs {
        let full_table = full_root.join(cfg.name);
        let uni_table = unified_root.join(cfg.name);
        let mut dates = BTreeSet::new();
        if full_table.exists() {
            for entry in fs::read_dir(&full_table)? {
                let entry = entry?;
                let name = entry.file_name().to_string_lossy().into_owned();
                if let Some(date) = name.strip_prefix("date=") {
                    dates.insert(date.to_string());
                }
            }
        }
        if uni_table.exists() {
            for entry in fs::read_dir(&uni_table)? {
                let entry = entry?;
                let name = entry.file_name().to_string_lossy().into_owned();
                if let Some(date) = name.strip_prefix("date=") {
                    dates.insert(date.to_string());
                }
            }
        }

        let mut deltas = Vec::new();
        let mut total_delta = 0i64;
        for date in dates {
            let full_files = parquet_files(&full_table.join(format!("date={date}")))?;
            let uni_files = parquet_files(&uni_table.join(format!("date={date}")))?;
            let full_rows = if full_files.is_empty() {
                0
            } else {
                count_rows(&con, &parquet_source(&full_files))?
            };
            let unified_rows = if uni_files.is_empty() {
                0
            } else {
                count_rows(&con, &parquet_source(&uni_files))?
            };
            if full_rows != unified_rows {
                let delta = full_rows as i64 - unified_rows as i64;
                total_delta += delta;
                if delta > 0 || unified_rows == 0 {
                    repairable_dates.insert(date.clone());
                }
                deltas.push(DateDelta {
                    date,
                    full_rows,
                    unified_rows,
                    delta_rows: delta,
                    full_shards: full_files.len(),
                    unified_shards: uni_files.len(),
                });
            }
        }
        deltas.sort_by(|a, b| b.delta_rows.cmp(&a.delta_rows));
        report_tables.push(TableScan {
            table: cfg.name.to_string(),
            dates_with_delta: deltas.len(),
            total_delta_rows: total_delta,
            dates: deltas,
        });
    }

    Ok(ScanReport {
        full_root: full_root.display().to_string(),
        unified_root: unified_root.display().to_string(),
        tables: report_tables,
        repairable_dates,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

struct RepairJob {
    table: &'static TableConfig,
    date: String,
    input_files: Vec<PathBuf>,
    output_path: PathBuf,
}

pub fn repair_partitions(
    full_root: &Path,
    unified_root: &Path,
    tables: &[&str],
    dates: Option<&BTreeSet<String>>,
    compression: &str,
    row_group_size: usize,
    parallel_jobs: usize,
) -> Result<RepairReport> {
    let started = Instant::now();
    let configs: Vec<&TableConfig> = if tables.is_empty() {
        TABLES.iter().collect()
    } else {
        tables
            .iter()
            .map(|name| table_by_name(name).with_context(|| format!("unknown table {name}")))
            .collect::<Result<Vec<_>>>()?
    };

    let scan = scan_partitions(full_root, unified_root, tables)?;
    let target_dates: BTreeSet<String> = dates
        .cloned()
        .unwrap_or_else(|| scan.repairable_dates.clone());

    let mut jobs = Vec::new();
    for cfg in configs {
        let full_table = full_root.join(cfg.name);
        let uni_table = unified_root.join(cfg.name);
        for date in &target_dates {
            let mut input_files = parquet_files(&full_table.join(format!("date={date}")))?;
            let uni_dir = uni_table.join(format!("date={date}"));
            let mut uni_files = parquet_files(&uni_dir)?;
            input_files.append(&mut uni_files);
            if input_files.is_empty() {
                continue;
            }
            input_files.sort();
            input_files.dedup();
            let out_dir = uni_table.join(format!("date={date}"));
            let output_path = out_dir.join("part-000001.parquet");
            jobs.push(RepairJob {
                table: cfg,
                date: date.clone(),
                input_files,
                output_path,
            });
        }
    }

    if jobs.is_empty() {
        bail!("no repair jobs found");
    }

    eprintln!("repair jobs={} parallel_jobs={parallel_jobs}", jobs.len());
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(parallel_jobs.max(1))
        .build()
        .context("rayon pool")?;
    let repaired: Vec<RepairResult> = pool.install(|| {
        jobs.par_iter()
            .map(|job| {
                let result = repair_one(job, compression, row_group_size);
                if let Ok(ref ok) = result {
                    eprintln!(
                        "  {} date={} status={} out_rows={}",
                        ok.table, ok.date, ok.status, ok.output_rows
                    );
                }
                result
            })
            .collect::<Result<Vec<_>>>()
    })?;

    Ok(RepairReport {
        repaired,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

fn repair_one(job: &RepairJob, compression: &str, row_group_size: usize) -> Result<RepairResult> {
    let con = Connection::open_in_memory().context("duckdb repair")?;
    let _ = con.execute("PRAGMA threads=2", []);

    let source = parquet_source(&job.input_files);
    let input_rows = count_rows(&con, &source)?;
    if input_rows == 0 {
        return Ok(RepairResult {
            table: job.table.name.to_string(),
            date: job.date.clone(),
            input_rows: 0,
            output_rows: 0,
            input_shards: job.input_files.len(),
            status: "empty".into(),
        });
    }

    fs::create_dir_all(job.output_path.parent().unwrap())?;
    let tmp_path = job.output_path.with_extension("parquet.tmp");
    let deduped = dedupe_sql(&source, job.table);
    let copy_sql = format!(
        "COPY ({deduped}) TO '{}' (FORMAT PARQUET, COMPRESSION '{compression}', ROW_GROUP_SIZE {row_group_size})",
        tmp_path.to_string_lossy()
    );
    con.execute(&copy_sql, []).with_context(|| {
        format!(
            "repair {} date={} -> {}",
            job.table.name,
            job.date,
            job.output_path.display()
        )
    })?;
    fs::rename(&tmp_path, &job.output_path)?;
    let output_rows = count_rows(&con, &format!("read_parquet('{}')", job.output_path.to_string_lossy()))?;

    Ok(RepairResult {
        table: job.table.name.to_string(),
        date: job.date.clone(),
        input_rows,
        output_rows,
        input_shards: job.input_files.len(),
        status: "ok".into(),
    })
}