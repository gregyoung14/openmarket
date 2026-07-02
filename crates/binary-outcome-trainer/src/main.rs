//! CLI: train binary outcome model from step3 CSV export.
//!
//! ```bash
//! cargo run -p binary-outcome-trainer --release -- \
//!   --input data/hf_release/features_exports/step3_binary_calibration_<ts>.csv \
//!   --artifact-dir data/ml_artifacts
//! ```

use std::path::PathBuf;

use anyhow::Result;
use binary_outcome_trainer::{load_dataset, train, TrainConfig};
use clap::Parser;

#[derive(Parser, Debug)]
#[command(name = "train_binary_outcome_model")]
#[command(about = "Walk-forward logistic regression trainer for step3 binary calibration CSV")]
struct Args {
    /// Step3 binary calibration CSV from export_step3_from_parquet
    #[arg(long)]
    input: PathBuf,

    /// Directory for model + metrics JSON artifacts
    #[arg(long, env = "ML_ARTIFACT_DIR", default_value = "data/ml_artifacts")]
    artifact_dir: PathBuf,

    #[arg(long, default_value_t = 12)]
    min_train_markets: usize,

    #[arg(long, default_value_t = 4)]
    test_markets: usize,

    #[arg(long, default_value_t = 4)]
    step_markets: usize,

    #[arg(long, default_value_t = 300)]
    epochs: usize,

    #[arg(long, default_value_t = 0.05)]
    lr: f64,

    #[arg(long, default_value_t = 0.01)]
    fee_rate: f64,

    #[arg(long, default_value_t = 0.005)]
    slippage: f64,

    #[arg(long, default_value_t = 0.0)]
    min_ev: f64,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let dataset = load_dataset(&args.input)?;
    let config = TrainConfig {
        min_train_markets: args.min_train_markets,
        test_markets: args.test_markets,
        step_markets: args.step_markets,
        epochs: args.epochs,
        lr: args.lr,
        fee_rate: args.fee_rate,
        slippage: args.slippage,
        min_ev: args.min_ev,
    };

    let summary = train(&config, &dataset, &args.input, &args.artifact_dir)?;

    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "artifact_path": summary.artifact_path,
            "latest_path": summary.latest_path,
            "metrics_path": summary.metrics_path,
            "markets_total": summary.markets_total,
            "rows_total": summary.rows_total,
            "elapsed_seconds": summary.elapsed_seconds,
            "metrics": summary.metrics,
        }))?
    );
    Ok(())
}