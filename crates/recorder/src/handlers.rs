use std::sync::atomic::Ordering;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::Query;
use axum::{extract::State, response::IntoResponse, Json};
use serde::Deserialize;
use serde_json::json;

use crate::db;
use crate::lag;
use crate::services::AppState;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

pub async fn health_handler(State(state): State<AppState>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "service": "market-data-recorder",
        "timestamp": now_ms(),
        "binance_ingested": state.binance_ingested.load(Ordering::Relaxed),
        "polymarket_ingested": state.polymarket_ingested.load(Ordering::Relaxed),
        "lag_pairs_written": state.lag_pairs_written.load(Ordering::Relaxed),
    }))
}

pub async fn stats_handler(State(state): State<AppState>) -> impl IntoResponse {
    let latest_market = state.last_market.read().await.clone();
    Json(json!({
        "service": "market-data-recorder",
        "timestamp": now_ms(),
        "counts": {
            "binance_ingested": state.binance_ingested.load(Ordering::Relaxed),
            "polymarket_ingested": state.polymarket_ingested.load(Ordering::Relaxed),
            "lag_pairs_written": state.lag_pairs_written.load(Ordering::Relaxed)
        },
        "latest_market": latest_market
    }))
}

pub async fn export_step1_handler() -> impl IntoResponse {
    match tokio::task::spawn_blocking(lag::export_step1_csv).await {
        Ok(Ok(path)) => Json(json!({"status": "ok", "path": path})),
        Ok(Err(e)) => Json(json!({"status": "error", "error": e.to_string()})),
        Err(e) => Json(json!({"status": "error", "error": e.to_string()})),
    }
}

pub async fn export_step2_handler() -> impl IntoResponse {
    match tokio::task::spawn_blocking(lag::export_step2_features_csv).await {
        Ok(Ok(path)) => Json(json!({"status": "ok", "path": path})),
        Ok(Err(e)) => Json(json!({"status": "error", "error": e.to_string()})),
        Err(e) => Json(json!({"status": "error", "error": e.to_string()})),
    }
}

pub async fn export_step2_hf_handler() -> impl IntoResponse {
    match tokio::task::spawn_blocking(lag::export_step2_hf_features_csv).await {
        Ok(Ok((path_100ms, path_1s))) => Json(json!({
            "status": "ok",
            "path_100ms": path_100ms,
            "path_1s": path_1s
        })),
        Ok(Err(e)) => Json(json!({"status": "error", "error": e.to_string()})),
        Err(e) => Json(json!({"status": "error", "error": e.to_string()})),
    }
}

#[derive(Debug, Deserialize)]
pub struct Step3Query {
    pub start_ts_ms: Option<i64>,
    pub end_ts_ms: Option<i64>,
    pub lookback_hours: Option<u64>,
    pub market_limit: Option<usize>,
}

pub async fn export_step3_binary_calibration_handler(
    Query(query): Query<Step3Query>,
) -> impl IntoResponse {
    let options = lag::Step3ExportOptions {
        start_ts_ms: query.start_ts_ms,
        end_ts_ms: query.end_ts_ms,
        lookback_hours: query.lookback_hours.unwrap_or(72),
        market_limit: query.market_limit,
    };

    match tokio::task::spawn_blocking(move || lag::export_step3_binary_calibration_csv(options))
        .await
    {
        Ok(Ok(summary)) => Json(json!({
            "status": "ok",
            "path": summary.csv_path,
            "manifest_path": summary.manifest_path,
            "markets": summary.markets,
            "rows": summary.rows,
            "ties_dropped": summary.ties_dropped,
        })),
        Ok(Err(e)) => Json(json!({"status": "error", "error": e.to_string()})),
        Err(e) => Json(json!({"status": "error", "error": e.to_string()})),
    }
}

pub async fn warm_state_handler(State(state): State<AppState>) -> impl IntoResponse {
    let res = tokio::task::spawn_blocking(|| {
        let conn = db::get_db_conn()?;
        db::get_latest_market_meta(&conn).map_err(anyhow::Error::from)
    })
    .await;

    match res {
        Ok(Ok(Some(meta))) => {
            {
                let mut lm = state.last_market.write().await;
                *lm = Some(meta.clone());
            }
            {
                let mut map = state.token_mapping.write().await;
                map.market_slug = Some(meta.market_slug.clone());
                map.up_token_id = Some(meta.up_token_id.clone());
                map.down_token_id = Some(meta.down_token_id.clone());
                map.asset_map.insert(
                    meta.up_token_id.clone(),
                    crate::services::AssetInfo {
                        side: "UP".to_string(),
                        market_slug: meta.market_slug.clone(),
                    },
                );
                map.asset_map.insert(
                    meta.down_token_id.clone(),
                    crate::services::AssetInfo {
                        side: "DOWN".to_string(),
                        market_slug: meta.market_slug.clone(),
                    },
                );
            }
            Json(json!({"status": "ok", "restored_market": meta.market_slug}))
        }
        Ok(Ok(None)) => Json(json!({"status": "ok", "restored_market": null})),
        Ok(Err(e)) => Json(json!({"status": "error", "error": e.to_string()})),
        Err(e) => Json(json!({"status": "error", "error": e.to_string()})),
    }
}
