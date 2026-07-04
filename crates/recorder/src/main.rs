use axum::{routing::get, Router};
use market_data_recorder::{config, db, handlers, ingest, lag, models, services};
use tokio::sync::mpsc;
use tracing::info;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    if let Err(e) = db::init_database() {
        tracing::error!("DB init failed: {}", e);
        return;
    }

    let (db_tx, db_rx) = mpsc::channel::<models::DbMessage>(config::DB_CHANNEL_SIZE);
    let state = services::AppState::new(db_tx);

    ingest::spawn_db_writer(db_rx);
    ingest::spawn_binance_ingestor(state.clone());
    ingest::spawn_polymarket_ingestor(state.clone());
    lag::spawn_lag_pairing_task(state.clone());

    // restore latest market metadata on startup
    let _ = handlers::warm_state_handler(axum::extract::State(state.clone())).await;

    // Pre-populate asset_map with ALL historical token→side mappings
    if let Ok(conn) = db::get_db_conn() {
        if let Ok(all_meta) = db::get_all_market_meta(&conn) {
            let mut map = state.token_mapping.write().await;
            for meta in &all_meta {
                map.asset_map.insert(
                    meta.up_token_id.clone(),
                    services::AssetInfo {
                        side: "UP".to_string(),
                        market_slug: meta.market_slug.clone(),
                    },
                );
                map.asset_map.insert(
                    meta.down_token_id.clone(),
                    services::AssetInfo {
                        side: "DOWN".to_string(),
                        market_slug: meta.market_slug.clone(),
                    },
                );
            }
            info!(
                "Pre-loaded {} market token mappings into asset_map ({} asset_ids)",
                all_meta.len(),
                map.asset_map.len()
            );
        }
    }

    let app = Router::new()
        .route("/health", get(handlers::health_handler))
        .route("/stats", get(handlers::stats_handler))
        .route("/warm-state", get(handlers::warm_state_handler))
        .route("/export/step1", get(handlers::export_step1_handler))
        .route("/export/step2", get(handlers::export_step2_handler))
        .route("/export/step2_hf", get(handlers::export_step2_hf_handler))
        .route(
            "/export/step3_binary_calibration",
            get(handlers::export_step3_binary_calibration_handler),
        )
        .with_state(state);

    let addr = format!("{}:{}", config::SERVER_HOST, config::server_port());
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    info!("market-data-recorder listening on {}", addr);
    axum::serve(listener, app).await.unwrap();
}
