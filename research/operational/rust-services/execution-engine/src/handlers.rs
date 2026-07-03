use std::sync::Arc;

use axum::{
    Json,
    extract::{State, WebSocketUpgrade, ws},
    response::Response,
};
use futures::{SinkExt, StreamExt};
use serde_json::json;

use crate::state::AppState;

/// GET /health
pub async fn health(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let pm = state.position_manager.lock();
    let wallet_usdc = state.wallet_balances.lock().usdc_e;
    Json(json!({
        "status": "ok",
        "service": "execution-engine",
        "execution_version": crate::config::EXECUTION_VERSION,
        "bankroll": wallet_usdc,
        "open_positions": pm.positions.len(),
        "total_trades": pm.closed_positions.len(),
        "clob_connected": state.clob_healthy.load(std::sync::atomic::Ordering::Relaxed),
        "wallet_address": state.wallet_address,
        "uptime_secs": state.start_time.elapsed().as_secs(),
    }))
}

/// GET /status
pub async fn status(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let pm = state.position_manager.lock();
    let total_trades = pm.closed_positions.len();
    let wins = pm
        .closed_positions
        .iter()
        .filter(|p| p.pnl.unwrap_or(0.0) > 0.0)
        .count();
    let losses = total_trades - wins;
    let total_pnl: f64 = pm
        .closed_positions
        .iter()
        .map(|p| p.pnl.unwrap_or(0.0))
        .sum();
    let drawdown_pct = 0.0; // No paper bankroll — wallet is source of truth

    let open: Vec<serde_json::Value> = pm
        .positions
        .iter()
        .map(|p| {
            json!({
                "id": p.id,
                "market": p.market_slug,
                "side": p.side.as_str(),
                "entry_price": p.entry_price,
                "shares": p.shares,
                "bet_amount": p.bet_amount,
                "confidence": p.confidence,
                "entry_time": p.entry_time.to_rfc3339(),
            })
        })
        .collect();

    let recent_closed: Vec<serde_json::Value> = pm
        .closed_positions
        .iter()
        .rev()
        .take(10)
        .map(|p| {
            json!({
                "id": p.id,
                "market": p.market_slug,
                "side": p.side.as_str(),
                "entry_price": p.entry_price,
                "exit_price": p.exit_price,
                "pnl": p.pnl,
                "exit_type": format!("{:?}", p.exit_type),
            })
        })
        .collect();

    let live_prices = state.live_prices.lock().clone();
    let market_slug = state
        .market_context
        .lock()
        .as_ref()
        .map(|m| m.slug.clone())
        .unwrap_or_default();
    let balances = state.wallet_balances.lock().clone();
    let wallet_usdc = balances.usdc_e;

    Json(json!({
        "bankroll": wallet_usdc,
        "peak_bankroll": wallet_usdc,
        "open_positions": open,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": if total_trades > 0 { wins as f64 / total_trades as f64 } else { 0.0 },
        "total_pnl": total_pnl,
        "drawdown_pct": drawdown_pct,
        "strategy": format!("{:?}", pm.strategy),
        "clob_connected": state.clob_healthy.load(std::sync::atomic::Ordering::Relaxed),
        "wallet_address": state.wallet_address,
        "uptime_secs": state.start_time.elapsed().as_secs(),
        "market_slug": market_slug,
        "prices": {
            "up_bid": live_prices.up_bid,
            "up_ask": live_prices.up_ask,
            "down_bid": live_prices.down_bid,
            "down_ask": live_prices.down_ask,
        },
        "wallet_balances": {
            "usdc_e": balances.usdc_e,
            "usdc_native": balances.usdc_native,
            "matic": balances.matic,
        },
        "recent_trades": recent_closed,
    }))
}

/// GET /ws — WebSocket endpoint for real-time execution events
pub async fn ws_handler(ws: WebSocketUpgrade, State(state): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws(socket, state))
}

async fn handle_ws(socket: ws::WebSocket, state: Arc<AppState>) {
    let (mut sender, mut receiver) = socket.split();

    // Send welcome message
    let welcome = json!({
        "type": "connected",
        "service": "execution-engine",
        "timestamp": chrono::Utc::now().timestamp_millis(),
    });
    let _ = sender.send(ws::Message::Text(welcome.to_string())).await;

    // Send current status
    let status_json = {
        let pm = state.position_manager.lock();
        let uptime_secs = state.start_time.elapsed().as_secs();
        let clob_connected = state
            .clob_healthy
            .load(std::sync::atomic::Ordering::Relaxed);
        let market_slug = state
            .market_context
            .lock()
            .as_ref()
            .map(|m| m.slug.clone())
            .unwrap_or_default();
        let live_prices = state.live_prices.lock().clone();
        let balances = state.wallet_balances.lock().clone();
        let status = pm.status_event(
            &state.wallet_address,
            uptime_secs,
            clob_connected,
            &market_slug,
            &live_prices,
            &balances,
        );
        serde_json::to_string(&status).ok()
    };
    if let Some(json) = status_json {
        let _ = sender.send(ws::Message::Text(json)).await;
    }

    // Subscribe to broadcast channel
    let mut rx = state.event_tx.subscribe();

    // Forward events to WS client
    let send_task = tokio::spawn(async move {
        while let Ok(event) = rx.recv().await {
            if let Ok(json) = serde_json::to_string(&event)
                && sender.send(ws::Message::Text(json)).await.is_err()
            {
                break;
            }
        }
    });

    // Drain incoming messages (we don't expect any, but handle close)
    let recv_task = tokio::spawn(async move {
        while let Some(Ok(_msg)) = receiver.next().await {
            // Client messages ignored
        }
    });

    // Wait for either task to finish
    tokio::select! {
        _ = send_task => {},
        _ = recv_task => {},
    }
}
