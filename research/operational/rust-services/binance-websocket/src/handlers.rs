use crate::{config, db, services::AppState};
use axum::{
    Json,
    extract::{
        Path, Query, State, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::info;

fn age_ms(now_ms: i64, timestamp_ms: Option<i64>) -> Option<i64> {
    timestamp_ms.map(|ts| now_ms.saturating_sub(ts))
}

/// WebSocket upgrade handler
pub async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> Response {
    ws.on_upgrade(|socket| handle_socket(socket, state))
}

/// Handle individual WebSocket connection
pub async fn handle_socket(mut socket: WebSocket, state: AppState) {
    info!("✅ WebSocket client connected");
    let mut rx = state.tx.subscribe();

    // Send initial snapshot of recent candles
    let snapshot_data = tokio::task::spawn_blocking(|| {
        if let Ok(conn) = db::get_db_conn() {
            let mut stmt = conn
                .prepare(
                    "SELECT candle_start, open_price, high_price, low_price, close_price, volume
                    FROM binance_candles_1m
                    ORDER BY candle_start DESC
                    LIMIT 50",
                )
                .ok()?;

            let candles = stmt
                .query_map([], |row| {
                    Ok(json!({
                        "time": row.get::<_, i64>(0)?,
                        "open": row.get::<_, f64>(1)?,
                        "high": row.get::<_, f64>(2)?,
                        "low": row.get::<_, f64>(3)?,
                        "close": row.get::<_, f64>(4)?,
                        "volume": row.get::<_, f64>(5)?,
                    }))
                })
                .ok()?
                .collect::<Result<Vec<_>, _>>()
                .ok()?;

            Some(candles.into_iter().rev().collect::<Vec<_>>())
        } else {
            None
        }
    })
    .await
    .unwrap();

    if let Some(candles) = snapshot_data {
        let msg = json!({
            "type": "snapshot",
            "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64,
            "data": candles
        })
        .to_string();
        let _ = socket.send(Message::Text(msg)).await;
    }

    // Stream updates to client
    loop {
        tokio::select! {
            Ok(msg) = rx.recv() => {
                if socket.send(Message::Text(msg)).await.is_err() {
                    break;
                }
            }
            Some(result) = socket.recv() => {
                if result.is_err() {
                    break;
                }
            }
        }
    }
    info!("❌ WebSocket client disconnected");
}

/// Health check endpoint
pub async fn health_check_handler(State(state): State<AppState>) -> impl IntoResponse {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;

    let ws_connected = state.binance_ws_connected();
    let last_upstream_trade_ms = state.last_upstream_trade_received_ms();
    let last_broadcast_trade_ms = state.last_trade_broadcast_ms();

    let db_snapshot = tokio::task::spawn_blocking(|| {
        let conn = db::get_db_conn().ok()?;
        let trades_stored: i64 = conn
            .query_row("SELECT COUNT(*) FROM binance_trades", [], |r| r.get(0))
            .unwrap_or(0);
        let last_db_write_ms: Option<i64> = conn
            .query_row("SELECT MAX(received_at) FROM binance_trades", [], |r| r.get(0))
            .ok()
            .flatten();
        let database_size_bytes = std::fs::metadata(config::DATABASE_FILE).map(|m| m.len()).ok();
        Some((trades_stored, last_db_write_ms, database_size_bytes))
    })
    .await
    .unwrap();

    let (trades_stored, last_db_write_ms, database_size_bytes) = match db_snapshot {
        Some(snapshot) => snapshot,
        None => {
            return Json(json!({
                "service": "binance-websocket",
                "status": "error",
                "timestamp": now_ms,
                "connections": {
                    "binance_ws": ws_connected,
                },
                "freshness": {
                    "last_upstream_trade_ms": last_upstream_trade_ms,
                    "upstream_trade_age_ms": age_ms(now_ms, last_upstream_trade_ms),
                    "last_trade_broadcast_ms": last_broadcast_trade_ms,
                    "trade_broadcast_age_ms": age_ms(now_ms, last_broadcast_trade_ms),
                    "last_db_write_ms": null,
                    "db_write_age_ms": null,
                },
                "storage": {
                    "database_file": config::DATABASE_FILE,
                    "database_size_bytes": null,
                }
            }));
        }
    };

    let upstream_trade_age_ms = age_ms(now_ms, last_upstream_trade_ms);
    let trade_broadcast_age_ms = age_ms(now_ms, last_broadcast_trade_ms);
    let db_write_age_ms = age_ms(now_ms, last_db_write_ms);

    let upstream_trade_fresh = upstream_trade_age_ms
        .is_some_and(|age| age <= (config::HEALTH_UPSTREAM_STALE_SECS as i64 * 1000));
    let trade_broadcast_fresh = trade_broadcast_age_ms
        .is_some_and(|age| age <= (config::HEALTH_BROADCAST_STALE_SECS as i64 * 1000));
    let db_write_fresh = db_write_age_ms
        .is_some_and(|age| age <= (config::HEALTH_DB_STALE_SECS as i64 * 1000));

    let status = if ws_connected && trades_stored == 0 && last_upstream_trade_ms.is_none() {
        "starting"
    } else if !ws_connected {
        "degraded"
    } else if !upstream_trade_fresh || !trade_broadcast_fresh || !db_write_fresh {
        "stale"
    } else {
        "ok"
    };

    Json(json!({
        "service": "binance-websocket",
        "status": status,
        "timestamp": now_ms,
        "trades_stored": trades_stored,
        "connections": {
            "binance_ws": ws_connected,
        },
        "freshness": {
            "last_upstream_trade_ms": last_upstream_trade_ms,
            "upstream_trade_age_ms": upstream_trade_age_ms,
            "upstream_trade_fresh": upstream_trade_fresh,
            "last_trade_broadcast_ms": last_broadcast_trade_ms,
            "trade_broadcast_age_ms": trade_broadcast_age_ms,
            "trade_broadcast_fresh": trade_broadcast_fresh,
            "last_db_write_ms": last_db_write_ms,
            "db_write_age_ms": db_write_age_ms,
            "db_write_fresh": db_write_fresh,
            "thresholds_secs": {
                "upstream_trade": config::HEALTH_UPSTREAM_STALE_SECS,
                "trade_broadcast": config::HEALTH_BROADCAST_STALE_SECS,
                "db_write": config::HEALTH_DB_STALE_SECS,
            }
        },
        "storage": {
            "database_file": config::DATABASE_FILE,
            "database_size_bytes": database_size_bytes,
        }
    }))
}

#[derive(Deserialize)]
pub struct CandlesQuery {
    pub limit: Option<u64>,
}

/// Get historical candles for a specific interval
pub async fn get_candles_handler(
    Path(interval): Path<String>,
    Query(params): Query<CandlesQuery>,
) -> impl IntoResponse {
    let limit = params.limit.unwrap_or(100).min(10000);

    if !config::VALID_INTERVALS.contains(&interval.as_str()) {
        return Json(json!({"error": "Invalid interval"}));
    }

    let interval_clone = interval.clone();
    let result = tokio::task::spawn_blocking(move || {
        if let Ok(conn) = db::get_db_conn() {
            let table = format!("binance_candles_{}", interval_clone);
            let sql = format!(
                "SELECT candle_start, open_price, high_price, low_price, close_price, volume, quote_volume, trade_count
                FROM {}
                ORDER BY candle_start DESC
                LIMIT ?",
                table
            );

            let mut stmt = conn.prepare(&sql).ok()?;
            let rows = stmt
                .query_map([limit], |row| {
                    Ok(json!({
                        "time": row.get::<_, i64>(0)?,
                        "open": row.get::<_, f64>(1)?,
                        "high": row.get::<_, f64>(2)?,
                        "low": row.get::<_, f64>(3)?,
                        "close": row.get::<_, f64>(4)?,
                        "volume": row.get::<_, f64>(5)?,
                        "quote_volume": row.get::<_, f64>(6)?,
                        "trade_count": row.get::<_, i64>(7)?
                    }))
                })
                .ok()?
                .collect::<Result<Vec<_>, _>>()
                .ok()?;

            Some(rows.into_iter().rev().collect::<Vec<_>>())
        } else {
            None
        }
    })
    .await
    .unwrap();

    match result {
        Some(candles) => Json(json!({
            "interval": interval,
            "count": candles.len(),
            "candles": candles
        })),
        None => Json(json!({"error": "DB Error"})),
    }
}
