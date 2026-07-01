//! Signal Engine v14.0.0 — v14 Production Signal Engine
//!
//! Architecture:
//! ```
//! ┌──────────────────────┐   WS    ┌─────────────────────────────────┐
//! │  Binance WS (8001)   │────────►│                                 │
//! └──────────────────────┘         │   Signal Engine v5 (8003)      │
//!                                  │                                 │
//! ┌──────────────────────┐   WS    │  upstream.rs → drift.rs → /ws  │
//! │  Polymarket WS (8002)│────────►│  scanner.rs (1s loop)          │
//! └──────────────────────┘         │  No Python — pure Rust math    │
//!                                  └──────────┬──────────────────────┘
//!                                             │ WS (8003/ws)
//!                                             ▼
//!                                  ┌──────────────────────┐
//!                                  │  Execution Engine    │
//!                                  │  (port 8004)         │
//!                                  └──────────────────────┘
//! ```
//!
//! Signal method: 3-component weighted drift estimator + regime gating + adaptive confirmation
//!   - Brownian drift P(UP) via z-score + norm CDF   (55%)
//!   - OFI acceleration: split-window detrended OFI  (30%)
//!   - Scoreboard: price vs open via sigmoid          (15%)
//!
//! Regime detection: path efficiency + lag-1 autocorrelation
//!   - trend: full confidence
//!   - neutral: -0.02 confidence penalty
//!   - chop: skip entirely (reset confirmation)
//!
//! Confidence ≥ 0.60 sustained for 15–50s (adaptive) → then:
//!   - Price cap: entry_ask ≤ 0.55
//!   - EV edge:   confidence - (ask + 0.005 slippage) ≥ 0.08
//!
//! Run order:
//!   1. Start Binance WS (port 8001)
//!   2. Start Polymarket WS (port 8002)
//!   3. Start Signal Engine (this service, port 8003)
//!   4. Start Execution Engine (subscribes to 8003/ws)

mod calibrated;
mod config;
mod drift;
mod handlers;
mod models;
mod scanner;
mod state;
mod upstream;
mod volume;

use axum::{routing::get, Router};
use btc_common::version;
use calibrated::{BinaryModelArtifact, ScorerMode};
use std::sync::Arc;
use tokio::sync::broadcast;
use tracing::{info, warn};

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    info!("═══════════════════════════════════════════════════════════");
    info!(
        "  Signal Engine {}  —  {}",
        version::SIGNAL_VERSION,
        version::SIGNAL_METHOD
    );
    info!("═══════════════════════════════════════════════════════════");
    info!("  Signal: weighted drift + whipsaw quality + regime gate");
    info!("    W_DRIFT:      {:.4}", config::W_DRIFT);
    info!("    W_OFI_ACCEL:  {:.4}", config::W_OFI_ACCEL);
    info!("    W_SCOREBOARD: {:.4}", config::W_SCOREBOARD);
    info!("    WHIPSAW_W:    {:.4}", config::WHIPSAW_WEIGHT);
    info!(
        "  Confirmation:  adaptive {}-{}s (base {}s), ≥{:.0}% confidence",
        config::MIN_CONFIRM_WINDOW,
        config::MAX_CONFIRM_WINDOW,
        config::BASE_CONFIRM_WINDOW,
        config::entry_confidence() * 100.0
    );
    info!("  Regime detection:");
    info!(
        "    Trend:   path_eff ≥ {:.2}, autocorr > -0.10",
        config::REGIME_TREND_THRESHOLD
    );
    info!(
        "    Chop:    path_eff < {:.2} or autocorr < {:.2}",
        config::REGIME_CHOP_THRESHOLD,
        config::REGIME_AUTOCORR_CHOP
    );
    info!("    Neutral: -0.02 confidence penalty");
    info!("  Entry filters:");
    info!(
        "    Max entry price:   {:.2}  (was 0.80)",
        config::max_entry_price()
    );
    info!(
        "    Min EV edge:       {:.2}  (was 0.05)",
        config::min_edge()
    );
    info!(
        "    Min confidence:    {:.2}  (was 0.55)",
        config::entry_confidence()
    );
    info!("    Slippage:          {:.3}", config::SLIPPAGE);
    info!("    Hour blacklist ET: {:?}", config::BLACKLIST_HOURS_ET);
    info!(
        "    Volume gate:       {}",
        if config::enable_volume_gate() {
            "enabled"
        } else {
            "disabled"
        }
    );
    info!(
        "  Entry window:  {}s – {}s into market",
        config::min_secs_into_market(),
        config::max_secs_into_market()
    );
    info!("═══════════════════════════════════════════════════════════");

    let requested_mode = ScorerMode::parse(&config::calibrated_scorer_mode());
    let requested_path = config::calibrated_model_path();
    let mut effective_mode = requested_mode;
    let calibrated_model = if requested_mode == ScorerMode::Disabled {
        None
    } else if let Some(path) = requested_path.clone() {
        match BinaryModelArtifact::load_from_path(&path) {
            Ok(artifact) => {
                info!(mode = requested_mode.as_str(), path = %path, artifact = %artifact.artifact_label(), "Loaded calibrated scorer artifact");
                Some(Arc::new(artifact))
            }
            Err(error) => {
                warn!(mode = requested_mode.as_str(), path = %path, error = %error, "Failed to load calibrated scorer artifact; falling back to disabled mode");
                effective_mode = ScorerMode::Disabled;
                None
            }
        }
    } else {
        warn!(mode = requested_mode.as_str(), "CALIBRATED_SCORER_MODE requested without CALIBRATED_MODEL_PATH; falling back to disabled mode");
        effective_mode = ScorerMode::Disabled;
        None
    };

    info!(requested_mode = requested_mode.as_str(), effective_mode = effective_mode.as_str(), model_path = ?requested_path, "Calibrated scorer configuration");

    // Broadcast channel for downstream signal consumers
    let (signal_tx, _) = broadcast::channel::<String>(config::BROADCAST_CHANNEL_SIZE);

    // Shared state
    let app_state =
        state::AppState::new_with_calibrated(signal_tx, calibrated_model, effective_mode);

    // Start upstream WebSocket connectors (no ML bridge needed)
    let state_binance = app_state.clone();
    tokio::spawn(upstream::binance_upstream_task(state_binance));

    let state_poly = app_state.clone();
    tokio::spawn(upstream::polymarket_upstream_task(state_poly));

    // Start the 1-second signal scanner
    scanner::spawn_signal_scanner(app_state.clone());

    // Build HTTP/WS router
    let app = Router::new()
        .route("/ws", get(handlers::ws_handler))
        .route("/health", get(handlers::health_handler))
        .route("/", get(handlers::root_handler))
        .with_state(app_state);

    let addr = format!("{}:{}", config::SERVER_HOST, config::server_port());
    info!("Starting Signal Engine on {}", addr);
    info!("  Binance upstream:     {}", config::BINANCE_WS_URL);
    info!("  Polymarket upstream:  {}", config::POLYMARKET_WS_URL);
    info!("  ML Bridge:            NONE (pure Rust signal engine)");

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    info!("✅ Listening on {}", addr);

    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .await
    .unwrap();
}
