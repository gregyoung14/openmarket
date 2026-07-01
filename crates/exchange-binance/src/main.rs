mod config;
mod db;
mod handlers;
mod models;
mod services;
mod tasks;
// mod ta;

use axum::{Router, routing::get};
use tokio::sync::{broadcast, mpsc};
use tracing::info;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    if let Err(e) = db::init_database() {
        tracing::error!("Failed to init database: {}", e);
        return;
    }

    // Channels
    let (tx_broadcast, _) = broadcast::channel::<String>(config::BROADCAST_CHANNEL_SIZE);
    let (tx_db_write, rx_db_write) = mpsc::channel::<models::Trade>(config::DB_WRITE_CHANNEL_SIZE);

    // Build application state so background tasks and handlers share freshness state
    let app_state = services::AppState::new(tx_broadcast.clone());

    // Spawn background tasks
    tasks::spawn_db_writer(rx_db_write);

    let tx_w = tx_db_write.clone();
    tokio::spawn(tasks::binance_reader_task(app_state.clone(), tx_w));

    let tx_b_agg = tx_broadcast.clone();
    tokio::spawn(tasks::aggregator_task(tx_b_agg));

    let app = Router::new()
        .route("/ws", get(handlers::ws_handler))
        .route("/candles/:interval", get(handlers::get_candles_handler))
        .route("/health", get(handlers::health_check_handler))
        .route("/", get(handlers::health_check_handler))
        .with_state(app_state);

    info!(
        "Starting Binance WebSocket service on {}:{}",
        config::SERVER_HOST,
        config::SERVER_PORT
    );

    let addr = format!("{}:{}", config::SERVER_HOST, config::SERVER_PORT);
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
