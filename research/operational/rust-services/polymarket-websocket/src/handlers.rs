use crate::config;
use crate::services::AppState;
use axum::{
    extract::ws::{WebSocket, WebSocketUpgrade},
    extract::State,
    response::IntoResponse,
    Json,
};
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};

fn age_ms(now_ms: i64, timestamp_ms: Option<i64>) -> Option<i64> {
    timestamp_ms.map(|ts| now_ms.saturating_sub(ts))
}

/// Health check endpoint with service status
pub async fn health_check_handler(State(state): State<AppState>) -> impl IntoResponse {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;

    let upstream_connected = state.upstream_connected();
    let current_market = state.get_market();
    let last_upstream_message_ms = state.last_upstream_message_ms();
    let last_market_data_ms = state.last_market_data_ms();
    let last_market_change_ms = state.last_market_change_ms();

    let upstream_message_age_ms = age_ms(timestamp, last_upstream_message_ms);
    let market_data_age_ms = age_ms(timestamp, last_market_data_ms);
    let market_change_age_ms = age_ms(timestamp, last_market_change_ms);

    let upstream_message_fresh = upstream_message_age_ms
        .is_some_and(|age| age <= (config::HEALTH_UPSTREAM_STALE_SECS as i64 * 1000));
    let market_data_fresh = market_data_age_ms
        .is_some_and(|age| age <= (config::HEALTH_MARKET_DATA_STALE_SECS as i64 * 1000));

    let status = if upstream_connected
        && (current_market.is_none()
            || last_upstream_message_ms.is_none()
            || last_market_data_ms.is_none())
    {
        "starting"
    } else if !upstream_connected {
        "degraded"
    } else if !upstream_message_fresh || !market_data_fresh {
        "stale"
    } else {
        "ok"
    };

    Json(json!({
        "service": "polymarket-websocket",
        "status": status,
        "timestamp": timestamp,
        "port": config::SERVER_PORT,
        "connections": {
            "upstream_ws": upstream_connected,
        },
        "market": {
            "current": current_market,
            "last_market_change_ms": last_market_change_ms,
            "market_change_age_ms": market_change_age_ms,
        },
        "freshness": {
            "last_upstream_message_ms": last_upstream_message_ms,
            "upstream_message_age_ms": upstream_message_age_ms,
            "upstream_message_fresh": upstream_message_fresh,
            "last_market_data_ms": last_market_data_ms,
            "market_data_age_ms": market_data_age_ms,
            "market_data_fresh": market_data_fresh,
            "thresholds_secs": {
                "upstream_message": config::HEALTH_UPSTREAM_STALE_SECS,
                "market_data": config::HEALTH_MARKET_DATA_STALE_SECS,
            }
        }
    }))
}

/// WebSocket handler for real-time market data
pub async fn ws_handler(State(state): State<AppState>, ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(|ws: WebSocket| handle_socket(ws, state))
}

/// Handle individual WebSocket connection
async fn handle_socket(mut ws: WebSocket, state: AppState) {
    // Subscribe to broadcast channel
    let mut rx = state.broadcast_tx.subscribe();

    // Send initial welcome message
    let _ = ws
        .send(axum::extract::ws::Message::Text(
            json!({
                "type": "connected",
                "service": "polymarket-websocket",
                "timestamp": SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64
            })
            .to_string(),
        ))
        .await;

    // Send current tracked market metadata (slug/token IDs) if available
    if let Some(market_msg) = state.get_market_message() {
        let _ = ws.send(axum::extract::ws::Message::Text(market_msg)).await;
    }

    // Broadcast loop
    loop {
        tokio::select! {
            Ok(msg) = rx.recv() => {
                if ws.send(axum::extract::ws::Message::Text(msg)).await.is_err() {
                    break;
                }
            }
            else => break,
        }
    }
}

/// Root endpoint
pub async fn root_handler() -> impl IntoResponse {
    Json(json!({
        "service": "polymarket-websocket",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "websocket": "/ws"
        }
    }))
}
