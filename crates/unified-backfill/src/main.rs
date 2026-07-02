//! Incremental repair of unified Parquet from full_parquet shards.
//!
//! ```bash
//! cargo run -p unified-backfill --release -- scan
//! cargo run -p unified-backfill --release -- repair --tables binance_trades,polymarket_ticks_ms
//! ```

use std::collections::BTreeSet;
use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};
use unified_backfill::{
    fill_missing_from_sqlite, market_calendar_dates, missing_partition_dates, repair_partitions,
    scan_partitions, ALL_TABLES,
};

#[derive(Parser, Debug)]
#[command(name = "unified-backfill")]
#[command(about = "Scan and repair unified Parquet partitions from full_parquet")]
struct Args {
    #[arg(long, default_value = "data/hf_release/full_parquet")]
    full_root: PathBuf,

    #[arg(long, default_value = "data/hf_release/unified_parquet")]
    unified_root: PathBuf,

    #[arg(long, default_value = "data/hf_release/staging")]
    staging_dir: PathBuf,

    #[arg(long, default_value = "zstd")]
    compression: String,

    #[arg(long, default_value_t = 100_000)]
    row_group_size: usize,

    /// Parallel partition repairs (keep low for large polymarket_ticks_ms merges)
    #[arg(long, default_value_t = 3)]
    jobs: usize,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Report row-count deltas between full and unified per date partition
    Scan {
        #[arg(long, default_value = "data/hf_release/metadata/backfill_scan.json")]
        report: PathBuf,
        #[arg(long, value_delimiter = ',')]
        tables: Vec<String>,
    },
    /// Export missing date partitions from staging SQLite into unified
    SqliteFill {
        #[arg(long, default_value = "data/hf_release/metadata/sqlite_fill.json")]
        report: PathBuf,
        #[arg(long, value_delimiter = ',')]
        tables: Vec<String>,
        #[arg(long, value_delimiter = ',')]
        dates: Vec<String>,
        #[arg(long, help = "Use market_meta calendar dates missing binance_trades partitions")]
        auto: bool,
    },
    /// Re-merge partitions where full_parquet has more data than unified
    Repair {
        #[arg(long, default_value = "data/hf_release/metadata/backfill_repair.json")]
        report: PathBuf,
        #[arg(long, value_delimiter = ',')]
        tables: Vec<String>,
        #[arg(long, value_delimiter = ',')]
        dates: Vec<String>,
    },
}

fn table_names(tables: &[String]) -> Vec<&str> {
    if tables.is_empty() {
        ALL_TABLES.iter().map(|t| t.name).collect()
    } else {
        tables.iter().map(|s| s.as_str()).collect()
    }
}

fn main() -> Result<()> {
    let args = Args::parse();
    match args.command {
        Command::Scan { report, tables } => {
            let names = table_names(&tables);
            let scan = scan_partitions(&args.full_root, &args.unified_root, &names)?;
            if let Some(parent) = report.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::write(&report, serde_json::to_string_pretty(&scan)?)?;
            println!("backfill scan ok ({:.1}s)", scan.elapsed_seconds);
            println!("report={}", report.display());
            println!("repairable_dates={}", scan.repairable_dates.len());
            for table in &scan.tables {
                if table.dates_with_delta > 0 {
                    println!(
                        "  {}: {} dates, +{} rows in full vs unified",
                        table.table, table.dates_with_delta, table.total_delta_rows
                    );
                }
            }
        }
        Command::SqliteFill {
            report,
            tables,
            dates,
            auto,
        } => {
            let names = table_names(&tables);
            let target_dates = if auto {
                let cal = market_calendar_dates(&args.unified_root)?;
                missing_partition_dates(&args.unified_root, "binance_trades", &cal)
            } else if dates.is_empty() {
                anyhow::bail!("pass --dates or --auto")
            } else {
                dates
            };
            eprintln!("sqlite-fill target_dates={}", target_dates.len());
            let fill = fill_missing_from_sqlite(
                &args.staging_dir,
                &args.unified_root,
                &names,
                &target_dates,
                &args.compression,
                args.row_group_size,
                args.jobs,
            )?;
            if let Some(parent) = report.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::write(&report, serde_json::to_string_pretty(&fill)?)?;
            let ok = fill.filled.iter().filter(|r| r.status == "ok").count();
            let rows: u64 = fill.filled.iter().map(|r| r.rows).sum();
            println!("sqlite-fill ok ({:.1}s)", fill.elapsed_seconds);
            println!("report={}", report.display());
            println!("partitions_filled={ok} rows={rows}");
        }
        Command::Repair { report, tables, dates } => {
            let names = table_names(&tables);
            let date_set = if dates.is_empty() {
                None
            } else {
                Some(dates.iter().cloned().collect::<BTreeSet<_>>())
            };
            let repair = repair_partitions(
                &args.full_root,
                &args.unified_root,
                &names,
                date_set.as_ref(),
                &args.compression,
                args.row_group_size,
                args.jobs,
            )?;
            if let Some(parent) = report.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::write(&report, serde_json::to_string_pretty(&repair)?)?;
            let ok = repair.repaired.iter().filter(|r| r.status == "ok").count();
            let rows: u64 = repair.repaired.iter().map(|r| r.output_rows).sum();
            println!("backfill repair ok ({:.1}s)", repair.elapsed_seconds);
            println!("report={}", report.display());
            println!("partitions_repaired={ok} output_rows={rows}");
        }
    }
    Ok(())
}