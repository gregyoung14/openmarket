//! Paper-Trade Executor (v2 - Audit-Verified Polymarket Integration)
//!
//! This module simulates BTC market trading against Polymarket with:
//! - Fixed WebSocket message handling (verified against official Polymarket docs)
//! - Quarter-Kelly (0.25x) position sizing
//! - $100 initial capital with PnL tracking  
//! - Gamma API resolution for accurate settlement
//! - Full audit compliance with POLYMARKET_INTEGRATION.md
//!
//! Connects to:
//!   - signal-engine (ws://127.0.0.1:8010/ws) for trading signals
//!   - polymarket-websocket (ws://127.0.0.1:8002/ws) for live prices
//!
//! Usage:
//!   paper-executor --signal-url ws://127.0.0.1:8010/ws \
//!     --strategy v14_baseline \
//!     --log paper_v14_baseline.csv
//!
//! References:
//!   - Audit: docs/official-docs-mcp/TDR-POLYMARKET-AUDIT.md
//!   - Root cause: docs/official-docs-mcp/POLYMARKET-INVERSION-ROOT-CAUSE.md
//!   - Integration specs: docs/official-docs-mcp/POLYMARKET_INTEGRATION.md

use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use clap::Parser;
use futures::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde::Deserialize;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

fn csv_opt_f64(value: Option<f64>) -> String {
    value.map(|v| format!("{:.6}", v)).unwrap_or_default()
}

fn csv_opt_str(value: Option<&str>) -> String {
    value.unwrap_or_default().to_string()
}

const CSV_HEADER: &str = "timestamp,strategy,slug,direction,confidence,edge,regime,entry_ask,\
result,pnl,bankroll,bankroll_pct_change,trades_total,trades_won,trades_lost,\
scoring_mode,ranking_basis,ranking_score,raw_model_prob_up,calibrated_prob_up,\
selected_side_prob,ev_up,ev_down,artifact_version";

// ────────────────────────────────────────────────────────────────────────────
// ──── Constants & Configuration ────────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

/// Initial bankroll in USD
const INITIAL_BANKROLL: f64 = 100.0;

/// Quarter-Kelly: for binary bet paying $1/share, full Kelly = edge / (1 - ask)
/// where edge = confidence - ask. We apply 0.25x multiplier for conservative sizing.
/// Reference: POLYMARKET_INTEGRATION.md "Implementation Notes"
const KELLY_MULTIPLIER: f64 = 0.25;

/// Min position size as fraction of bankroll
const MIN_KELLY_FRACTION: f64 = 0.001; // 0.1%

/// Max position size as fraction of bankroll
const MAX_KELLY_FRACTION: f64 = 0.05; // 5%

/// Slippage assumption (bid-ask spread padding)
/// Reference: POLYMARKET_INTEGRATION.md "Paper Trading Simulation"
const SLIPPAGE: f64 = 0.005; // 0.5%

/// Fee rate assumption (Polymarket has variable fees 0.06%-1.56%)
/// We use 1% as conservative average estimate
/// Reference: POLYMARKET_INTEGRATION.md footnote on official fee table
const FEE_RATE: f64 = 0.01;

/// Settlement threshold - outcome price >= 0.95 = winner
/// Reference: POLYMARKET_INTEGRATION.md "Resolution Lookup (Gamma API)"
const SETTLEMENT_THRESHOLD: f64 = 0.95;

/// BTC market duration: 15 minutes = 900 seconds
/// Reference: POLYMARKET_INTEGRATION.md "Token Mapping Strategy"
const MARKET_DURATION_SECS: i64 = 900;

/// Delay before querying Gamma API (allow time for oracle resolution)
/// Reference: POLYMARKET_INTEGRATION.md "Resolution via Gamma API"
const GAMMA_RESOLVE_DELAY_MS: i64 = 2 * 60 * 1000; // 2 minutes

// ────────────────────────────────────────────────────────────────────────────
// ──── CLI Arguments ────────────────────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(
    name = "paper-executor",
    about = "Polymarket Paper Trading Simulator (Audit-Verified v2)"
)]
struct Args {
    /// Signal engine WebSocket URL
    #[arg(long)]
    signal_url: String,

    /// Strategy name (v14_baseline, v14.1_no_volgate, v14_relaxed_conf, v14_tight_regime, v14_wide_confirm, v15_brier_cb)
    #[arg(long)]
    strategy: String,

    /// Output CSV file for trade log
    #[arg(long)]
    log: PathBuf,

    /// Polymarket price feed WebSocket URL
    #[arg(long, default_value = "ws://127.0.0.1:8002/ws")]
    price_url: String,
}

// ────────────────────────────────────────────────────────────────────────────
// ──── Model Types ─────────────────────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

/// Signal message from signal-engine
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum SignalMessage {
    Connected {
        #[allow(dead_code)]
        service: Option<String>,
    },
    Entry(Box<EntrySignal>),
    #[serde(other)]
    Other,
}

/// Entry signal with trade parameters
#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
struct EntrySignal {
    direction: String,
    confidence: f64,
    #[serde(default)]
    consistency: Option<f64>,
    #[serde(default)]
    market: Option<String>,
    #[serde(default)]
    secs_in: Option<u64>,
    #[serde(default)]
    secs_left: Option<u64>,
    #[serde(default)]
    entry_ask: Option<f64>,
    #[serde(default)]
    entry_bid: Option<f64>,
    #[serde(default)]
    edge: Option<f64>,
    #[serde(default)]
    regime: Option<String>,
    #[serde(default)]
    adaptive_confirm: Option<u64>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    timestamp: Option<i64>,
    #[serde(default)]
    scoring_mode: Option<String>,
    #[serde(default)]
    ranking_basis: Option<String>,
    #[serde(default)]
    ranking_score: Option<f64>,
    #[serde(default)]
    raw_model_prob_up: Option<f64>,
    #[serde(default)]
    calibrated_prob_up: Option<f64>,
    #[serde(default)]
    selected_side_prob: Option<f64>,
    #[serde(default)]
    ev_up: Option<f64>,
    #[serde(default)]
    ev_down: Option<f64>,
    #[serde(default)]
    artifact_version: Option<String>,
}

/// Pending trade awaiting resolution
#[derive(Debug, Clone)]
struct PendingTrade {
    slug: String,
    direction: String,
    confidence: f64,
    edge: f64,
    regime: String,
    entry_ask: f64,
    stake: f64,
    shares: f64,
    market_end_ms: i64,
    scoring_mode: Option<String>,
    ranking_basis: Option<String>,
    ranking_score: Option<f64>,
    raw_model_prob_up: Option<f64>,
    calibrated_prob_up: Option<f64>,
    selected_side_prob: Option<f64>,
    ev_up: Option<f64>,
    ev_down: Option<f64>,
    artifact_version: Option<String>,
}

/// Outcome from Gamma API
#[derive(Debug, Clone, Copy, PartialEq)]
enum SettledOutcome {
    UpWon,
    DownWon,
}

// ────────────────────────────────────────────────────────────────────────────
// ──── Paper Trading State ─────────────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

/// Tracks paper trading portfolio and statistics
struct PaperState {
    /// Current bankroll in USD (starts at $100.00)
    bankroll: f64,

    /// Initial bankroll for statistics
    initial_bankroll: f64,

    /// Strategy identifier
    strategy: String,

    /// CSV log file path
    log_path: PathBuf,

    /// Whether CSV header has been written
    csv_initialized: bool,

    /// Markets already traded (prevent duplicates)
    traded_markets: std::collections::HashSet<String>,

    /// Trade statistics
    trades_total: u64,
    trades_won: u64,
    trades_lost: u64,
    pnl_total: f64,
}

impl PaperState {
    /// Create new paper state with $100 initial bankroll
    fn new(strategy: String, log_path: PathBuf) -> Self {
        Self {
            bankroll: INITIAL_BANKROLL,
            initial_bankroll: INITIAL_BANKROLL,
            strategy,
            log_path,
            csv_initialized: false,
            traded_markets: std::collections::HashSet::new(),
            trades_total: 0,
            trades_won: 0,
            trades_lost: 0,
            pnl_total: 0.0,
        }
    }

    /// Initialize CSV file with header
    fn write_csv_header(&mut self) {
        if self.csv_initialized {
            return;
        }

        if self.log_path.exists() {
            match fs::File::open(&self.log_path) {
                Ok(file) => {
                    let mut reader = BufReader::new(file);
                    let mut first_line = String::new();
                    if reader.read_line(&mut first_line).is_ok() {
                        let existing_header = first_line.trim_end_matches(['\r', '\n']);
                        if !existing_header.is_empty() && existing_header != CSV_HEADER {
                            let rotated_path = self.log_path.with_extension(format!(
                                "legacy-{}.csv",
                                Utc::now().timestamp_millis()
                            ));
                            if let Err(e) = fs::rename(&self.log_path, &rotated_path) {
                                error!(
                                    path = ?self.log_path,
                                    rotated_path = ?rotated_path,
                                    error = %e,
                                    "Failed to rotate CSV with incompatible header"
                                );
                                return;
                            }

                            warn!(
                                path = ?self.log_path,
                                rotated_path = ?rotated_path,
                                "Rotated CSV with incompatible header before writing new schema"
                            );
                        } else if !existing_header.is_empty() {
                            self.csv_initialized = true;
                            return;
                        }
                    }
                }
                Err(e) => {
                    error!(path = ?self.log_path, error = %e, "Failed to inspect CSV header");
                    return;
                }
            }
        }

        let mut file = match OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&self.log_path)
        {
            Ok(f) => f,
            Err(e) => {
                error!(path = ?self.log_path, error = %e, "Failed to open CSV");
                return;
            }
        };

        let _ = writeln!(file, "{}", CSV_HEADER);
        self.csv_initialized = true;
    }

    /// Process entry signal (Kelly sizing, trade logging)
    ///
    /// Implements Quarter-Kelly for binary bets:
    ///   kelly_fraction = KELLY_MULTIPLIER * edge / (1 - ask)
    ///
    /// Reference: TDR-POLYMARKET-AUDIT.md "Kelly Sizing Section"
    fn process_entry(&mut self, entry: &EntrySignal, market_end_ms: i64) -> Option<PendingTrade> {
        let slug = entry.market.as_deref().unwrap_or("unknown");

        // Prevent duplicate trades
        if self.traded_markets.contains(slug) {
            debug!(slug, "Already traded this market — skipping");
            return None;
        }

        // Validate entry price
        let entry_ask = match entry.entry_ask {
            Some(price) if price > 0.0 && price.is_finite() => price,
            _ => {
                warn!(slug, entry_ask = ?entry.entry_ask, "Invalid entry_ask — skipping");
                return None;
            }
        };

        // Calculate position size using Quarter-Kelly for binary bets
        // Full Kelly: f* = edge / (1 - ask), where edge = confidence - ask
        // Quarter-Kelly: f = edge / (4 * (1 - ask))
        let edge = entry.edge.unwrap_or(0.1).max(0.0);
        let confidence = entry.confidence.clamp(0.0, 1.0);

        let kelly_raw = KELLY_MULTIPLIER * edge / (1.0 - entry_ask);
        let kelly_fraction = kelly_raw.clamp(MIN_KELLY_FRACTION, MAX_KELLY_FRACTION);

        let bet_amount = self.bankroll * kelly_fraction;
        let shares = bet_amount / entry_ask;

        if bet_amount <= 0.0 || !bet_amount.is_finite() || shares <= 0.0 || !shares.is_finite() {
            warn!(
                slug,
                bankroll = self.bankroll,
                bet_amount,
                kelly_fraction,
                shares,
                "Invalid position size — skipping"
            );
            return None;
        }

        // Log entry in CSV
        self.write_csv_header();
        let ts = entry
            .timestamp
            .unwrap_or_else(|| Utc::now().timestamp_millis());
        let regime = entry.regime.as_deref().unwrap_or("unknown");
        let scoring_mode = entry.scoring_mode.as_deref();
        let ranking_basis = entry.ranking_basis.as_deref();

        let mut file = match OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_path)
        {
            Ok(f) => f,
            Err(e) => {
                error!(path = ?self.log_path, error = %e, "Failed to open CSV");
                return None;
            }
        };

        // Write PENDING row (will be updated at resolution)
        let pending_row = vec![
            ts.to_string(),
            self.strategy.clone(),
            slug.to_string(),
            entry.direction.clone(),
            format!("{:.4}", confidence),
            format!("{:.4}", edge),
            regime.to_string(),
            format!("{:.4}", entry_ask),
            "PENDING".to_string(),
            "0.00".to_string(),
            format!("{:.2}", self.bankroll),
            "0.00%".to_string(),
            self.trades_total.to_string(),
            self.trades_won.to_string(),
            self.trades_lost.to_string(),
            csv_opt_str(scoring_mode),
            csv_opt_str(ranking_basis),
            csv_opt_f64(entry.ranking_score),
            csv_opt_f64(entry.raw_model_prob_up),
            csv_opt_f64(entry.calibrated_prob_up),
            csv_opt_f64(entry.selected_side_prob),
            csv_opt_f64(entry.ev_up),
            csv_opt_f64(entry.ev_down),
            csv_opt_str(entry.artifact_version.as_deref()),
        ];
        let _ = writeln!(file, "{}", pending_row.join(","));

        info!(
            strategy = %self.strategy,
            slug,
            direction = %entry.direction,
            confidence = format!("{:.1}%", confidence * 100.0),
            edge = format!("{:.3}", edge),
            scorer_mode = ?entry.scoring_mode,
            ranking_basis = ?entry.ranking_basis,
            kelly_pct = format!("{:.2}%", kelly_fraction * 100.0),
            position_size = format!("${:.2}", bet_amount),
            shares = format!("{:.4}", shares),
            bankroll_before = format!("${:.2}", self.bankroll),
            "📝 Entry signal processed"
        );

        self.traded_markets.insert(slug.to_string());

        Some(PendingTrade {
            slug: slug.to_string(),
            direction: entry.direction.clone(),
            confidence,
            edge,
            regime: regime.to_string(),
            entry_ask,
            stake: bet_amount,
            shares,
            market_end_ms,
            scoring_mode: entry.scoring_mode.clone(),
            ranking_basis: entry.ranking_basis.clone(),
            ranking_score: entry.ranking_score,
            raw_model_prob_up: entry.raw_model_prob_up,
            calibrated_prob_up: entry.calibrated_prob_up,
            selected_side_prob: entry.selected_side_prob,
            ev_up: entry.ev_up,
            ev_down: entry.ev_down,
            artifact_version: entry.artifact_version.clone(),
        })
    }

    /// Resolve trade based on market outcome
    ///
    /// Determines WIN/LOSS based on final Gamma API prices.
    /// Updates bankroll with PnL.
    ///
    /// Reference: POLYMARKET_INTEGRATION.md "Resolution via Gamma API"
    fn resolve_trade(&mut self, trade: &PendingTrade, outcome: SettledOutcome) {
        // Determine if direction matches outcome
        let direction_upper = trade.direction.to_uppercase();
        let won = match (&direction_upper[..], outcome) {
            ("UP", SettledOutcome::UpWon) => true,
            ("DOWN", SettledOutcome::DownWon) => true,
            _ => false,
        };

        let payout = if won { trade.shares } else { 0.0 };
        let entry_fee = trade.stake * FEE_RATE;
        let exit_fee = payout * FEE_RATE;
        let pnl = payout - trade.stake - entry_fee - exit_fee;

        self.bankroll += pnl;
        self.pnl_total += pnl;
        self.trades_total += 1;

        if won {
            self.trades_won += 1;
        } else {
            self.trades_lost += 1;
        }

        let result = if won { "WIN" } else { "LOSS" };
        let bankroll_change_pct = (pnl / (self.bankroll - pnl).max(1.0)) * 100.0;

        info!(
            strategy = %self.strategy,
            slug = %trade.slug,
            result,
            pnl = format!("{:+.2}", pnl),
            bankroll_after = format!("${:.2}", self.bankroll),
            pct_change = format!("{:+.2}%", bankroll_change_pct),
            stake = format!("${:.2}", trade.stake),
            shares = format!("{:.4}", trade.shares),
            outcome = ?outcome,
            "📊 Trade resolved"
        );

        // Append resolved row to CSV
        self.write_csv_header();
        let ts = Utc::now().timestamp_millis();

        let mut file = match OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_path)
        {
            Ok(f) => f,
            Err(e) => {
                error!(path = ?self.log_path, error = %e, "Failed to open CSV for resolution");
                return;
            }
        };

        let resolved_row = vec![
            ts.to_string(),
            self.strategy.clone(),
            trade.slug.clone(),
            trade.direction.clone(),
            format!("{:.4}", trade.confidence),
            format!("{:.4}", trade.edge),
            trade.regime.clone(),
            format!("{:.4}", trade.entry_ask),
            result.to_string(),
            format!("{:.2}", pnl),
            format!("{:.2}", self.bankroll),
            format!("{:.2}%", bankroll_change_pct),
            self.trades_total.to_string(),
            self.trades_won.to_string(),
            self.trades_lost.to_string(),
            csv_opt_str(trade.scoring_mode.as_deref()),
            csv_opt_str(trade.ranking_basis.as_deref()),
            csv_opt_f64(trade.ranking_score),
            csv_opt_f64(trade.raw_model_prob_up),
            csv_opt_f64(trade.calibrated_prob_up),
            csv_opt_f64(trade.selected_side_prob),
            csv_opt_f64(trade.ev_up),
            csv_opt_f64(trade.ev_down),
            csv_opt_str(trade.artifact_version.as_deref()),
        ];
        let _ = writeln!(file, "{}", resolved_row.join(","));
    }

    /// Get current performance metrics
    fn metrics_summary(&self) -> String {
        if self.trades_total == 0 {
            return "No trades yet".to_string();
        }
        let win_rate = (self.trades_won as f64 / self.trades_total as f64) * 100.0;
        let pnl_pct = ((self.bankroll - self.initial_bankroll) / self.initial_bankroll) * 100.0;
        format!(
            "Trades: {}/{} wins ({:.1}%) | Bankroll: ${:.2} ({:+.2}%) | PnL: ${:+.2}",
            self.trades_won, self.trades_total, win_rate, self.bankroll, pnl_pct, self.pnl_total
        )
    }
}

// ────────────────────────────────────────────────────────────────────────────
// ──── Polymarket WebSocket Handling ────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

/// Gamma API encodes `outcomes` and `outcomePrices` as JSON-within-JSON strings,
/// e.g. `"[\"Up\", \"Down\"]"` instead of a native JSON array.  `.as_array()`
/// always returns None on those fields, so we must try the string path first.
fn parse_gamma_json_array(v: &serde_json::Value) -> Vec<serde_json::Value> {
    if let Some(arr) = v.as_array() {
        arr.clone()
    } else if let Some(s) = v.as_str() {
        serde_json::from_str::<Vec<serde_json::Value>>(s).unwrap_or_default()
    } else {
        vec![]
    }
}

/// Parse a settled outcome from a single Gamma API market object.
///
/// Extracted as a pure function so it can be unit-tested against the real
/// API payload shape without making any HTTP calls.
///
/// Reference: POLYMARKET_INTEGRATION.md "Resolution via Gamma API"
fn parse_gamma_market_outcome(market: &serde_json::Value) -> Option<SettledOutcome> {
    // Parse outcomePrices — string decimals like "1" or "0.003"
    let prices: Vec<f64> = market
        .get("outcomePrices")
        .map(parse_gamma_json_array)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| {
            v.as_str()
                .and_then(|s| s.parse::<f64>().ok())
                .or_else(|| v.as_f64())
        })
        .collect();

    if prices.len() < 2 {
        return None;
    }

    // Parse outcomes to identify which index is UP/YES
    let outcomes: Vec<String> = market
        .get("outcomes")
        .map(parse_gamma_json_array)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().map(|s| s.trim().to_ascii_lowercase()))
        .collect();

    let up_idx: usize = if outcomes.len() >= 2 {
        outcomes
            .iter()
            .position(|o| o == "yes" || o == "up" || o.contains("higher") || o.contains("above"))
            .unwrap_or(0)
    } else {
        0
    };

    if prices[up_idx] >= SETTLEMENT_THRESHOLD {
        Some(SettledOutcome::UpWon)
    } else if prices[1 - up_idx] >= SETTLEMENT_THRESHOLD {
        Some(SettledOutcome::DownWon)
    } else {
        None
    }
}

/// Query Gamma API for settled market outcome
///
/// Official Polymarket documentation:
/// https://docs.polymarket.com/api-reference/markets/
///
/// Parses outcomes and outcomePrices to determine which side won.
/// Settlement threshold: price >= 0.95 = winner
///
/// Reference: POLYMARKET_INTEGRATION.md "Resolution Lookup (Gamma API)"
async fn fetch_market_outcome(slug: &str) -> Option<SettledOutcome> {
    let url = format!("https://gamma-api.polymarket.com/events?slug={}", slug);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .ok()?;

    let data: serde_json::Value = client.get(&url).send().await.ok()?.json().await.ok()?;

    // Navigate to the first market object
    let market = data
        .as_array()
        .and_then(|arr| arr.first())
        .and_then(|event| event.get("markets"))
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first())?;

    let outcome = parse_gamma_market_outcome(market);

    if outcome.is_some() {
        debug!(slug, "Gamma API outcome received");
    }

    outcome
}

/// Run price feed consumer
///
/// Implements all audit fixes:
/// 1. Listens for new_market event to populate token_side_map
/// 2. Iterates price_changes[] array (not single fields)
/// 3. Uses only token_side_map for book parsing, no fallback to "side" field
///
/// References:
///   - Audit: TDR-POLYMARKET-AUDIT.md "Fix #1-3"
///   - Inversion: POLYMARKET-INVERSION-ROOT-CAUSE.md "The Fallback Chain That Broke Everything"
async fn run_price_feed(
    url: &str,
    up_bid: Arc<Mutex<f64>>,
    down_bid: Arc<Mutex<f64>>,
    token_side_map: Arc<Mutex<HashMap<String, String>>>,
) {
    let mut delay = Duration::from_millis(500);

    loop {
        info!(url, "Connecting to polymarket price feed");

        match connect_async(url).await {
            Ok((ws_stream, _)) => {
                info!("Connected to polymarket price feed");
                delay = Duration::from_millis(500);

                let (mut _write, mut read) = ws_stream.split();

                while let Some(msg_result) = read.next().await {
                    match msg_result {
                        Ok(Message::Text(text)) => {
                            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&text) {
                                let msg_type = val
                                    .get("type")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_lowercase();

                                match msg_type.as_str() {
                                    // ──── FIX #1: Listen for official new_market event ────
                                    // Official Polymarket docs:
                                    // https://docs.polymarket.com/market-data/websocket/market-channel#new_market
                                    // Requires custom_feature_enabled: true in WebSocket subscription
                                    // Token ordering guaranteed by CTF:
                                    // https://docs.polymarket.com/quickstart
                                    // "The first ID is the Yes token, the second is the No token"
                                    // Also: https://docs.polymarket.com/trading/ctf/overview
                                    "new_market" => {
                                        if let Some(ids) =
                                            val.get("assets_ids").and_then(|v| v.as_array())
                                        {
                                            let id_strs: Vec<String> = ids
                                                .iter()
                                                .filter_map(|v| v.as_str().map(String::from))
                                                .collect();

                                            if id_strs.len() >= 2 {
                                                let mut map = token_side_map.lock();
                                                map.clear();
                                                map.insert(id_strs[0].clone(), "UP".to_string());
                                                map.insert(id_strs[1].clone(), "DOWN".to_string());

                                                info!(
                                                    up = &id_strs[0][..8.min(id_strs[0].len())],
                                                    down = &id_strs[1][..8.min(id_strs[1].len())],
                                                    "✅ Token map populated from new_market event \
                                                    (CTF platform-guaranteed ordering)"
                                                );
                                            }
                                        }
                                    }

                                    // ──── FIX #2: Iterate price_changes array ────
                                    // Official Polymarket docs:
                                    // https://docs.polymarket.com/market-data/websocket/market-channel#price_change
                                    // price_change message contains price_changes[] ARRAY
                                    // NOT top-level asset_id, best_bid fields
                                    "price_change" => {
                                        if let Some(changes) =
                                            val.get("price_changes").and_then(|v| v.as_array())
                                        {
                                            for change in changes {
                                                let asset_id = change
                                                    .get("asset_id")
                                                    .and_then(|v| v.as_str())
                                                    .unwrap_or("");

                                                let side =
                                                    token_side_map.lock().get(asset_id).cloned();

                                                let Some(side) = side else {
                                                    continue;
                                                };

                                                if let Some(bid) = change
                                                    .get("best_bid")
                                                    .and_then(|v| {
                                                        v.as_f64().or_else(|| {
                                                            v.as_str().and_then(|s| s.parse().ok())
                                                        })
                                                    })
                                                    .filter(|v| v.is_finite() && *v > 0.0)
                                                {
                                                    match side.as_str() {
                                                        "UP" => {
                                                            let mut b = up_bid.lock();
                                                            *b = bid;
                                                        }
                                                        "DOWN" => {
                                                            let mut b = down_bid.lock();
                                                            *b = bid;
                                                        }
                                                        _ => {}
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    "last_trade_price" => {
                                        let asset_id = val
                                            .get("asset_id")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("");

                                        let side = token_side_map.lock().get(asset_id).cloned();

                                        let Some(side) = side else {
                                            continue;
                                        };

                                        if let Some(price) = val
                                            .get("price")
                                            .and_then(|v| {
                                                v.as_f64().or_else(|| {
                                                    v.as_str().and_then(|s| s.parse().ok())
                                                })
                                            })
                                            .filter(|v| v.is_finite() && *v > 0.0)
                                        {
                                            match side.as_str() {
                                                "UP" => *up_bid.lock() = price,
                                                "DOWN" => *down_bid.lock() = price,
                                                _ => {}
                                            }
                                        }
                                    }

                                    // ──── FIX #3: Use only token_side_map, no "side" field fallback ────
                                    // Official Polymarket docs:
                                    // https://docs.polymarket.com/trading/orderbook
                                    // book message has: asset_id, bids[], asks[]
                                    // Does NOT have a "side" field
                                    // Using token_side_map is the only reliable approach
                                    "book" => {
                                        let asset_id = val
                                            .get("asset_id")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("");

                                        let side = token_side_map.lock().get(asset_id).cloned();

                                        let Some(side) = side else {
                                            continue;
                                        };

                                        if let Some(bids) =
                                            val.get("bids").and_then(|v| v.as_array())
                                        {
                                            let best = bids
                                                .iter()
                                                .filter_map(|row| {
                                                    row.as_array().and_then(|a| {
                                                        a.first().and_then(|p| {
                                                            p.as_f64().or_else(|| {
                                                                p.as_str()
                                                                    .and_then(|s| s.parse().ok())
                                                            })
                                                        })
                                                    })
                                                })
                                                .fold(0.0_f64, f64::max);

                                            if best > 0.0 {
                                                match side.as_str() {
                                                    "UP" => *up_bid.lock() = best,
                                                    "DOWN" => *down_bid.lock() = best,
                                                    _ => {}
                                                }
                                            }
                                        }
                                    }

                                    _ => {}
                                }
                            }
                        }
                        Ok(Message::Ping(data)) => {
                            let _ = _write.send(Message::Pong(data)).await;
                        }
                        Ok(Message::Close(_)) => break,
                        Err(e) => {
                            error!(error = %e, "Price feed error");
                            break;
                        }
                        _ => {}
                    }
                }
            }
            Err(e) => {
                error!(error = %e, "Failed to connect to price feed");
            }
        }

        warn!(delay_ms = delay.as_millis(), "Reconnecting to price feed");
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(30));
    }
}

// ────────────────────────────────────────────────────────────────────────────
// ──── Main ─────────────────────────────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "paper_executor=info".into()),
        )
        .init();

    let args = Args::parse();

    info!("═════════════════════════════════════════════════════════════");
    info!("  Paper-Trade Executor (Audit-Verified v2)");
    info!("  🧪 Simulation mode - no real orders placed");
    info!("─────────────────────────────────────────────────────────────");
    info!("  Initial Bankroll: ${:.2}", INITIAL_BANKROLL);
    info!(
        "  Kelly Sizing: Quarter-Kelly ({:.2}x multiplier)",
        KELLY_MULTIPLIER
    );
    info!("  Strategy: {}", args.strategy);
    info!("  Signal URL: {}", args.signal_url);
    info!("  Price URL: {}", args.price_url);
    info!("  Log File: {}", args.log.display());
    info!("─────────────────────────────────────────────────────────────");
    info!("  Audit References:");
    info!("    • TDR-POLYMARKET-AUDIT.md");
    info!("    • POLYMARKET-INVERSION-ROOT-CAUSE.md");
    info!("    • POLYMARKET_INTEGRATION.md");
    info!("═════════════════════════════════════════════════════════════");

    let state = Arc::new(Mutex::new(PaperState::new(
        args.strategy.clone(),
        args.log.clone(),
    )));

    let pending = Arc::new(Mutex::new(Vec::<PendingTrade>::new()));

    // Live prices from polymarket-websocket
    let up_bid = Arc::new(Mutex::new(0.0_f64));
    let down_bid = Arc::new(Mutex::new(0.0_f64));

    // Token ID → "UP"/"DOWN" mapping (populated from new_market events)
    let token_side_map: Arc<Mutex<HashMap<String, String>>> = Arc::new(Mutex::new(HashMap::new()));

    // Spawn price feed consumer
    {
        let price_url = args.price_url.clone();
        let up_bid = Arc::clone(&up_bid);
        let down_bid = Arc::clone(&down_bid);
        let token_side_map = Arc::clone(&token_side_map);
        tokio::spawn(async move {
            run_price_feed(&price_url, up_bid, down_bid, token_side_map).await;
        });
    }

    // Spawn resolution checker (every 10s)
    {
        let state = Arc::clone(&state);
        let pending = Arc::clone(&pending);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(10)).await;
                let now_ms = Utc::now().timestamp_millis();

                let mut to_resolve = Vec::new();
                {
                    let mut trades = pending.lock();
                    let mut i = 0;
                    while i < trades.len() {
                        if now_ms >= trades[i].market_end_ms + GAMMA_RESOLVE_DELAY_MS {
                            to_resolve.push(trades.remove(i));
                        } else {
                            i += 1;
                        }
                    }
                }

                for trade in to_resolve {
                    match fetch_market_outcome(&trade.slug).await {
                        Some(outcome) => {
                            info!(slug = %trade.slug, "Gamma API outcome received");
                            state.lock().resolve_trade(&trade, outcome);
                        }
                        None => {
                            warn!(slug = %trade.slug, "Gamma API outcome unavailable");
                        }
                    }
                }
            }
        });
    }

    // Main signal consumer loop
    let mut delay = Duration::from_millis(500);
    loop {
        info!(url = %args.signal_url, "Connecting to signal engine");

        match connect_async(&args.signal_url).await {
            Ok((ws_stream, _)) => {
                info!("✅ Connected to signal engine");
                delay = Duration::from_millis(500);

                let (mut _write, mut read) = ws_stream.split();

                while let Some(msg_result) = read.next().await {
                    match msg_result {
                        Ok(Message::Text(text)) => {
                            match serde_json::from_str::<SignalMessage>(&text) {
                                Ok(SignalMessage::Entry(entry)) => {
                                    let slug =
                                        entry.market.as_deref().unwrap_or("unknown").to_string();

                                    let market_end_ms = slug
                                        .rsplit('-')
                                        .next()
                                        .and_then(|s| s.parse::<i64>().ok())
                                        .map(|start| (start + MARKET_DURATION_SECS) * 1000)
                                        .unwrap_or(0);

                                    {
                                        let mut s = state.lock();
                                        if let Some(pt) = s.process_entry(&entry, market_end_ms) {
                                            pending.lock().push(pt);
                                        }
                                        info!("{}", s.metrics_summary());
                                    }
                                }
                                Ok(SignalMessage::Connected { .. }) => {
                                    info!("Signal engine handshake complete");
                                }
                                Ok(SignalMessage::Other) => {}
                                Err(e) => {
                                    debug!(error = %e, "Unparseable message");
                                }
                            }
                        }
                        Ok(Message::Ping(data)) => {
                            let _ = _write.send(Message::Pong(data)).await;
                        }
                        Ok(Message::Close(_)) => {
                            warn!("Signal engine WS closed");
                            break;
                        }
                        Err(e) => {
                            error!(error = %e, "Signal engine WS error");
                            break;
                        }
                        _ => {}
                    }
                }
            }
            Err(e) => {
                error!(error = %e, "Failed to connect to signal engine");
            }
        }

        warn!(delay_ms = delay.as_millis(), "Reconnecting");
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(30));
    }
}

// ────────────────────────────────────────────────────────────────────────────
// ──── Tests ────────────────────────────────────────────────────────────────
// ────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_initial_bankroll() {
        let state = PaperState::new("test".to_string(), PathBuf::from("test.csv"));
        assert_eq!(state.bankroll, INITIAL_BANKROLL);
        assert_eq!(state.initial_bankroll, INITIAL_BANKROLL);
        assert_eq!(state.bankroll, 100.0);
    }

    #[test]
    fn test_kelly_fraction_bounds() {
        let edge = 0.1;
        let entry_ask = 0.50;
        let kelly_raw = KELLY_MULTIPLIER * edge / (1.0 - entry_ask);
        let kelly_fraction = kelly_raw.clamp(MIN_KELLY_FRACTION, MAX_KELLY_FRACTION);

        assert!(kelly_fraction >= MIN_KELLY_FRACTION);
        assert!(kelly_fraction <= MAX_KELLY_FRACTION);
        assert!(kelly_fraction.is_finite());
    }

    #[test]
    fn test_kelly_multiplier_is_quarter_kelly() {
        assert_eq!(KELLY_MULTIPLIER, 0.25);
    }

    #[test]
    fn test_settlement_threshold() {
        assert_eq!(SETTLEMENT_THRESHOLD, 0.95);
    }

    #[test]
    fn test_market_duration() {
        assert_eq!(MARKET_DURATION_SECS, 900);
        assert_eq!(MARKET_DURATION_SECS / 60, 15); // 15 minutes
    }

    #[test]
    fn test_gamma_resolve_delay() {
        assert_eq!(GAMMA_RESOLVE_DELAY_MS, 2 * 60 * 1000);
    }

    #[test]
    fn test_position_size_calculation() {
        let bankroll = 100.0;
        let edge = 0.1;
        let entry_ask = 0.50;

        let kelly_raw = KELLY_MULTIPLIER * edge / (1.0 - entry_ask);
        let kelly_fraction = kelly_raw.clamp(MIN_KELLY_FRACTION, MAX_KELLY_FRACTION);
        let position = bankroll * kelly_fraction;

        // 0.25 * 0.1 / 0.5 = 0.05 (5% of bankroll), capped at MAX
        assert!(position > 0.0);
        assert!(position <= bankroll * MAX_KELLY_FRACTION);
    }

    #[test]
    fn test_share_count_from_entry_price() {
        let stake = 2.0;
        let entry_ask = 0.4;
        let shares = stake / entry_ask;

        assert_eq!(shares, 5.0);
    }

    #[test]
    fn test_pnl_calculation_win_share_based() {
        let stake = 2.0;
        let shares = 5.0;
        let payout = shares;
        let pnl = payout - stake - (stake * FEE_RATE) - (payout * FEE_RATE);

        assert!((pnl - 2.93).abs() < 1e-9);
    }

    #[test]
    fn test_pnl_calculation_loss_share_based() {
        let stake = 2.0;
        let payout = 0.0;
        let pnl = payout - stake - (stake * FEE_RATE);

        assert!((pnl - (-2.02)).abs() < 1e-9);
    }

    #[test]
    fn test_traded_markets_dedup() {
        let mut state = PaperState::new("test".to_string(), PathBuf::from("test.csv"));
        assert!(state.traded_markets.is_empty());

        state.traded_markets.insert("BTC-2026-03-18".to_string());
        assert!(state.traded_markets.contains("BTC-2026-03-18"));
        assert!(!state.traded_markets.contains("BTC-2026-03-19"));
    }

    #[test]
    fn test_statistics_initialized_empty() {
        let state = PaperState::new("test".to_string(), PathBuf::from("test.csv"));
        assert_eq!(state.trades_total, 0);
        assert_eq!(state.trades_won, 0);
        assert_eq!(state.trades_lost, 0);
        assert_eq!(state.pnl_total, 0.0);
    }

    #[test]
    fn test_kelly_conservative_with_low_edge() {
        let kelly_raw = KELLY_MULTIPLIER * 0.002 / (1.0 - 0.50); // Very low edge
        let kelly_fraction = kelly_raw.clamp(MIN_KELLY_FRACTION, MAX_KELLY_FRACTION);

        assert!(kelly_fraction >= MIN_KELLY_FRACTION);
        // 0.25 * 0.002 / 0.5 = 0.001 = MIN
        assert_eq!(kelly_fraction, MIN_KELLY_FRACTION);
    }

    #[test]
    fn test_kelly_capped_at_max() {
        let kelly_raw = KELLY_MULTIPLIER * 0.5 / (1.0 - 0.50); // Large edge, mid ask
        let kelly_fraction = kelly_raw.clamp(MIN_KELLY_FRACTION, MAX_KELLY_FRACTION);

        assert!(kelly_fraction <= MAX_KELLY_FRACTION);
        // 0.25 * 0.5 / 0.5 = 0.25 > 0.05, so capped at MAX
        assert_eq!(kelly_fraction, MAX_KELLY_FRACTION);
    }

    #[test]
    fn test_metrics_summary_no_trades() {
        let state = PaperState::new("test".to_string(), PathBuf::from("test.csv"));
        let summary = state.metrics_summary();
        assert!(summary.contains("No trades"));
    }

    #[test]
    fn test_fee_rate_applied() {
        let base_payout = 100.0;
        let fee = FEE_RATE;
        let after_fee = base_payout * (1.0 - fee);

        assert_eq!(after_fee, 99.0);
        assert_eq!(fee, 0.01);
    }

    #[test]
    fn test_slippage_assumption() {
        let bid = 0.50;
        let ask_with_slippage = bid + SLIPPAGE;

        assert_eq!(ask_with_slippage, 0.505);
        assert_eq!(SLIPPAGE, 0.005);
    }

    #[test]
    fn test_min_kelly_fraction() {
        assert_eq!(MIN_KELLY_FRACTION, 0.001);
    }

    #[test]
    fn test_max_kelly_fraction() {
        assert_eq!(MAX_KELLY_FRACTION, 0.05);
    }

    #[test]
    fn test_bankroll_stays_positive_after_loss() {
        let initial = 100.0;
        let loss = -2.02;
        let new_bankroll = initial + loss;

        assert!(new_bankroll > 0.0);
        assert!((new_bankroll - 97.98_f64).abs() < 1e-9);
    }

    #[test]
    fn test_bankroll_increases_after_binary_win() {
        let initial = 100.0;
        let win = 2.93;
        let new_bankroll = initial + win;

        assert!(new_bankroll > initial);
        assert!((new_bankroll - 102.93_f64).abs() < 1e-9);
    }

    #[test]
    fn test_write_csv_header_rotates_legacy_header() {
        let temp_dir = std::env::temp_dir().join(format!(
            "paper-executor-test-{}",
            Utc::now().timestamp_nanos_opt().unwrap_or_default()
        ));
        std::fs::create_dir_all(&temp_dir).unwrap();
        let log_path = temp_dir.join("paper_log_test.csv");
        std::fs::write(
            &log_path,
            "timestamp,strategy,slug,direction,confidence,edge,regime,entry_ask,result,pnl,bankroll,bankroll_pct_change,trades_total,trades_won,trades_lost\n",
        )
        .unwrap();

        let mut state = PaperState::new("test".to_string(), log_path.clone());
        state.write_csv_header();

        let current = std::fs::read_to_string(&log_path).unwrap();
        assert_eq!(current.trim_end(), CSV_HEADER);

        let rotated: Vec<_> = std::fs::read_dir(&temp_dir)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .filter(|name| name.contains("legacy-"))
            .collect();
        assert_eq!(rotated.len(), 1);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[test]
    fn test_outcome_direction_matching_up() {
        let outcome = SettledOutcome::UpWon;
        let direction = "UP";
        let won = matches!((direction, outcome), ("UP", SettledOutcome::UpWon));

        assert!(won);
    }

    #[test]
    fn test_outcome_direction_matching_down() {
        let outcome = SettledOutcome::DownWon;
        let direction = "DOWN";
        let won = matches!((direction, outcome), ("DOWN", SettledOutcome::DownWon));

        assert!(won);
    }

    #[test]
    fn test_outcome_direction_mismatch() {
        let outcome = SettledOutcome::UpWon;
        let direction = "DOWN";
        let won = matches!((direction, outcome), ("UP", SettledOutcome::UpWon));

        assert!(!won);
    }

    // ── Gamma API parsing ────────────────────────────────────────────────────
    //
    // The live API returns `outcomes` and `outcomePrices` as JSON-encoded
    // strings, NOT as native JSON arrays:
    //
    //   "outcomes": "[\"Up\", \"Down\"]"       ← string, not array
    //   "outcomePrices": "[\"1\", \"0\"]"      ← string, not array
    //
    // This was the root cause of every trade staying PENDING for 16 hours:
    // `.as_array()` always returned None on those fields.  These tests pin
    // the exact real API shape so it can never regress silently.

    /// Build a minimal Gamma API market object using the REAL wire format
    /// observed from https://gamma-api.polymarket.com/events?slug=…
    fn gamma_market(outcomes_str: &str, prices_str: &str) -> serde_json::Value {
        serde_json::json!({
            "outcomes": outcomes_str,
            "outcomePrices": prices_str
        })
    }

    #[test]
    fn test_gamma_up_won_string_encoded_arrays() {
        // Real payload shape: Up resolved, Down did not
        let market = gamma_market(r#"["Up", "Down"]"#, r#"["1", "0"]"#);
        assert_eq!(
            parse_gamma_market_outcome(&market),
            Some(SettledOutcome::UpWon)
        );
    }

    #[test]
    fn test_gamma_down_won_string_encoded_arrays() {
        // Real payload shape: Down resolved, Up did not
        let market = gamma_market(r#"["Up", "Down"]"#, r#"["0", "1"]"#);
        assert_eq!(
            parse_gamma_market_outcome(&market),
            Some(SettledOutcome::DownWon)
        );
    }

    #[test]
    fn test_gamma_up_won_decimal_prices() {
        // Near-settled: prices are decimal strings like "0.997" / "0.003"
        let market = gamma_market(r#"["Up", "Down"]"#, r#"["0.997", "0.003"]"#);
        assert_eq!(
            parse_gamma_market_outcome(&market),
            Some(SettledOutcome::UpWon)
        );
    }

    #[test]
    fn test_gamma_down_won_decimal_prices() {
        let market = gamma_market(r#"["Up", "Down"]"#, r#"["0.003", "0.997"]"#);
        assert_eq!(
            parse_gamma_market_outcome(&market),
            Some(SettledOutcome::DownWon)
        );
    }

    #[test]
    fn test_gamma_unresolved_returns_none() {
        // Mid-market prices — neither side at threshold yet
        let market = gamma_market(r#"["Up", "Down"]"#, r#"["0.55", "0.45"]"#);
        assert_eq!(parse_gamma_market_outcome(&market), None);
    }

    #[test]
    fn test_gamma_native_array_format_still_works() {
        // Belt-and-suspenders: if the API ever returns real JSON arrays, we
        // must still handle them correctly.
        let market = serde_json::json!({
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["1", "0"]
        });
        assert_eq!(
            parse_gamma_market_outcome(&market),
            Some(SettledOutcome::UpWon)
        );
    }

    #[test]
    fn test_gamma_missing_fields_returns_none() {
        let market = serde_json::json!({});
        assert_eq!(parse_gamma_market_outcome(&market), None);
    }

    #[test]
    fn test_gamma_too_few_prices_returns_none() {
        let market = serde_json::json!({
            "outcomes": r#"["Up"]"#,
            "outcomePrices": r#"["1"]"#
        });
        assert_eq!(parse_gamma_market_outcome(&market), None);
    }

    #[test]
    fn test_parse_gamma_json_array_from_string() {
        let v = serde_json::Value::String(r#"["Up", "Down"]"#.to_string());
        let result = parse_gamma_json_array(&v);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].as_str(), Some("Up"));
        assert_eq!(result[1].as_str(), Some("Down"));
    }

    #[test]
    fn test_parse_gamma_json_array_from_native_array() {
        let v = serde_json::json!(["Up", "Down"]);
        let result = parse_gamma_json_array(&v);
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn test_parse_gamma_json_array_invalid_string_returns_empty() {
        let v = serde_json::Value::String("not json".to_string());
        let result = parse_gamma_json_array(&v);
        assert!(result.is_empty());
    }
}
