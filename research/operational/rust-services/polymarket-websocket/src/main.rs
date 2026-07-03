mod config;
mod handlers;
mod models;
mod polymarket;
mod services;

use axum::{routing::get, Router};
use tokio::sync::broadcast;
use tracing::info;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    // Channels
    let (tx_broadcast, _) = broadcast::channel::<String>(config::BROADCAST_CHANNEL_SIZE);

    // Build application state
    let app_state = services::AppState::new(tx_broadcast.clone());

    // Spawn Polymarket reader task
    let tx_b = tx_broadcast.clone();
    let state_for_task = app_state.clone();
    tokio::spawn(polymarket::polymarket_reader_task(tx_b, state_for_task));

    // Build router
    let app = Router::new()
        .route("/ws", get(handlers::ws_handler))
        .route("/health", get(handlers::health_check_handler))
        .route("/", get(handlers::root_handler))
        .with_state(app_state);

    info!(
        "Starting Polymarket WebSocket service on {}:{}",
        config::SERVER_HOST,
        config::SERVER_PORT
    );

    let addr = format!("{}:{}", config::SERVER_HOST, config::SERVER_PORT);
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();

    info!("✅ Listening on {}", addr);

    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .await
    .unwrap();
}
