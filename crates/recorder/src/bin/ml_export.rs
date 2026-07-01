//! Offline ML feature exporter for archived SQLite snapshots.
//!
//! Usage:
//!   DATABASE_FILE=data/hf_release/staging/foo.db \
//!   ML_EXPORT_DIR=data/hf_release/features_exports \
//!   ARCHIVE_EXPORT=1 \
//!   cargo run -p market-data-recorder --bin ml_export -- step2_hf
//!
//!   DATABASE_FILE=... ARCHIVE_EXPORT=1 \
//!   cargo run -p market-data-recorder --bin ml_export -- step3

use anyhow::{bail, Result};
use market_data_recorder::lag::{self, Step3ExportOptions};
use std::env;

fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let step = env::args().nth(1).unwrap_or_else(|| "step2_hf".to_string());
    match step.as_str() {
        "step2_hf" => {
            let (path_100ms, path_1s) = lag::export_step2_hf_features_csv()?;
            println!("step2_hf ok");
            println!("path_100ms={path_100ms}");
            println!("path_1s={path_1s}");
        }
        "step3" => {
            let summary = lag::export_step3_binary_calibration_csv(Step3ExportOptions {
                start_ts_ms: None,
                end_ts_ms: None,
                lookback_hours: 72,
                market_limit: None,
            })?;
            println!("step3 ok");
            println!("csv_path={}", summary.csv_path);
            println!("manifest_path={}", summary.manifest_path);
            println!("markets={}", summary.markets);
            println!("rows={}", summary.rows);
            println!("ties_dropped={}", summary.ties_dropped);
        }
        other => bail!("unknown step {other:?}; expected step2_hf or step3"),
    }
    Ok(())
}