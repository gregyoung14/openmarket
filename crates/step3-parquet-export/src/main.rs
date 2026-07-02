//! CLI: export step3 binary calibration CSV from unified Parquet.
//!
//! ```bash
//! cargo run -p step3-parquet-export --release -- \
//!   --parquet-root data/hf_release/unified_parquet \
//!   --out-dir data/hf_release/features_exports
//! ```

use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use step3_parquet_export::{audit_step3, export_step3, ExportOptions};

#[derive(Parser, Debug)]
#[command(name = "export_step3_from_parquet")]
#[command(about = "Export step3 binary calibration features from unified Parquet")]
struct Args {
    /// Root of unified Parquet split (contains market_meta/, binance_trades/, …)
    #[arg(long, default_value = "data/hf_release/unified_parquet")]
    parquet_root: PathBuf,

    /// Directory for step3 CSV + manifest
    #[arg(long, default_value = "data/hf_release/features_exports")]
    out_dir: PathBuf,

    /// Optional lower bound on market start (epoch ms)
    #[arg(long)]
    start_ts_ms: Option<i64>,

    /// Optional upper bound on market start (epoch ms)
    #[arg(long)]
    end_ts_ms: Option<i64>,

    /// Keep only the last N markets after filtering (smoke tests)
    #[arg(long)]
    market_limit: Option<usize>,

    /// Audit export coverage without writing CSV (writes JSON report to --out-dir)
    #[arg(long)]
    audit: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let opts = ExportOptions {
        parquet_root: args.parquet_root,
        out_dir: args.out_dir,
        start_ts_ms: args.start_ts_ms,
        end_ts_ms: args.end_ts_ms,
        market_limit: args.market_limit,
    };

    if args.audit {
        let audit = audit_step3(opts.clone())?;
        std::fs::create_dir_all(&opts.out_dir)?;
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        let report_path = opts
            .out_dir
            .join(format!("step3_coverage_audit_{ts}.json"));
        std::fs::write(&report_path, serde_json::to_string_pretty(&audit)?)?;
        println!("step3 coverage audit ok");
        println!("report_path={}", report_path.display());
        println!("markets_selected={}", audit.markets_selected);
        println!("markets_written={}", audit.markets_written);
        println!("rows_written={}", audit.rows_written);
        println!("reason_counts={}", serde_json::to_string(&audit.reason_counts)?);
        println!(
            "recoverable={}",
            serde_json::to_string(&audit.recoverable)?
        );
        return Ok(());
    }

    let summary = export_step3(opts)?;

    println!("step3 parquet export ok");
    println!("csv_path={}", summary.csv_path);
    println!("manifest_path={}", summary.manifest_path);
    println!("markets_selected={}", summary.markets_selected);
    println!("markets_written={}", summary.markets_written);
    println!("rows={}", summary.rows_written);
    println!("ties_dropped={}", summary.ties_dropped);
    println!("elapsed_seconds={:.2}", summary.elapsed_seconds);
    Ok(())
}