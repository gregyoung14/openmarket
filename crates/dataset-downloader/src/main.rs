use anyhow::{Context, Result};
use chrono::{Duration, NaiveDate, Utc};
use clap::Parser;
use futures::stream::{self, StreamExt};
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};
use std::io::Cursor;
use std::path::{Path, PathBuf};
use tokio::fs;

const BASE_URL: &str = "https://data.binance.vision/data/spot/daily/klines";

#[derive(Parser, Debug)]
#[command(name = "binance-downloader")]
#[command(about = "Download Binance historical 1s kline data")]
struct Args {
    /// Trading pair symbol
    #[arg(short, long, default_value = "BTCUSDC")]
    symbol: String,

    /// Kline interval
    #[arg(short, long, default_value = "1s")]
    interval: String,

    /// Number of days back from today to download
    #[arg(short, long, default_value_t = 60)]
    days: u32,

    /// Output directory for CSV files
    #[arg(short, long, default_value = "data/raw")]
    output: PathBuf,

    /// Max concurrent downloads
    #[arg(short, long, default_value_t = 10)]
    concurrency: usize,
}

/// Build the URL for a given date
fn build_url(symbol: &str, interval: &str, date: &NaiveDate) -> String {
    let date_str = date.format("%Y-%m-%d");
    format!("{BASE_URL}/{symbol}/{interval}/{symbol}-{interval}-{date_str}.zip")
}

/// Build the expected CSV filename for a given date
fn csv_filename(symbol: &str, interval: &str, date: &NaiveDate) -> String {
    let date_str = date.format("%Y-%m-%d");
    format!("{symbol}-{interval}-{date_str}.csv")
}

/// Download and extract a single day's zip file
async fn download_day(
    client: &reqwest::Client,
    symbol: &str,
    interval: &str,
    date: NaiveDate,
    output_dir: &Path,
    pb: ProgressBar,
) -> Result<()> {
    let csv_name = csv_filename(symbol, interval, &date);
    let csv_path = output_dir.join(&csv_name);

    // Skip if already downloaded
    if csv_path.exists() {
        pb.finish_with_message(format!("{} ✓ (cached)", date));
        return Ok(());
    }

    let url = build_url(symbol, interval, &date);
    pb.set_message(format!("{} downloading...", date));

    let response = client.get(&url).send().await?;

    if !response.status().is_success() {
        pb.finish_with_message(format!("{} ✗ ({})", date, response.status()));
        return Ok(()); // Skip missing days (e.g. future dates)
    }

    let bytes = response.bytes().await?;
    pb.set_message(format!("{} extracting...", date));

    // Extract CSV from zip in memory
    let cursor = Cursor::new(bytes);
    let mut archive =
        zip::ZipArchive::new(cursor).with_context(|| format!("Failed to read zip for {date}"))?;

    for i in 0..archive.len() {
        let mut file = archive.by_index(i)?;
        let name = file.name().to_string();

        if name.ends_with(".csv") {
            let mut contents = Vec::new();
            std::io::copy(&mut file, &mut contents)?;
            fs::write(&csv_path, &contents).await?;
            break;
        }
    }

    pb.finish_with_message(format!("{} ✓", date));
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    // Create output directory
    fs::create_dir_all(&args.output).await?;

    // Build list of dates going backwards from yesterday (today may not be available yet)
    let today = Utc::now().date_naive();
    let dates: Vec<NaiveDate> = (1..=args.days)
        .map(|i| today - Duration::days(i as i64))
        .collect();

    println!(
        "📥 Downloading {} days of {}-{} data",
        dates.len(),
        args.symbol,
        args.interval
    );
    println!(
        "   Range: {} → {}",
        dates.last().unwrap(),
        dates.first().unwrap()
    );
    println!("   Output: {}", args.output.display());
    println!("   Concurrency: {}", args.concurrency);
    println!();

    let client = reqwest::Client::builder()
        .user_agent("binance-backtest-downloader/0.1")
        .build()?;

    let multi_progress = MultiProgress::new();
    let style = ProgressStyle::with_template("{msg}").unwrap();

    // Download all days concurrently (bounded)
    let results: Vec<Result<()>> = stream::iter(dates)
        .map(|date| {
            let client = &client;
            let symbol = args.symbol.clone();
            let interval = args.interval.clone();
            let output_dir = args.output.clone();
            let pb = multi_progress.add(ProgressBar::new_spinner());
            pb.set_style(style.clone());

            async move { download_day(client, &symbol, &interval, date, &output_dir, pb).await }
        })
        .buffer_unordered(args.concurrency)
        .collect()
        .await;

    // Report results
    let errors: Vec<_> = results.iter().filter(|r| r.is_err()).collect();
    let success_count = results.len() - errors.len();

    println!();
    println!(
        "✅ Downloaded {success_count}/{} days successfully",
        results.len()
    );

    if !errors.is_empty() {
        println!("⚠️  {} errors:", errors.len());
        for err in &errors {
            if let Err(e) = err {
                println!("   {e}");
            }
        }
    }

    // Count total CSV files
    let mut csv_count = 0;
    let mut total_size: u64 = 0;
    let mut entries = fs::read_dir(&args.output).await?;
    while let Some(entry) = entries.next_entry().await? {
        if entry.file_name().to_string_lossy().ends_with(".csv") {
            csv_count += 1;
            total_size += entry.metadata().await?.len();
        }
    }

    println!(
        "📁 {csv_count} CSV files in {} ({:.1} MB total)",
        args.output.display(),
        total_size as f64 / 1_048_576.0
    );

    Ok(())
}
