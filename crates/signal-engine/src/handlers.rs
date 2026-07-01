//! HTTP & WebSocket handlers for Signal Engine v4.2 (v14 production).

use crate::state::AppState;
use axum::{
    extract::{
        ws::{Message, WebSocket},
        State, WebSocketUpgrade,
    },
    response::IntoResponse,
    Json,
};
use btc_common::version;
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::info;

fn age_ms(now_ms: i64, timestamp_ms: Option<i64>) -> Option<i64> {
    timestamp_ms.map(|ts| now_ms.saturating_sub(ts))
}

/// Health check with full engine stats
pub async fn health_handler(State(state): State<AppState>) -> impl IntoResponse {
    let stats = state.get_stats();
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;

    let last_binance_trade_age_ms = age_ms(timestamp, stats.last_binance_trade_time);
    let last_polymarket_data_age_ms = age_ms(timestamp, stats.last_polymarket_data_time);
    let last_market_info_age_ms = age_ms(timestamp, stats.last_market_info_time);

    let binance_fresh = last_binance_trade_age_ms
        .is_some_and(|age| age <= (crate::config::HEALTH_BINANCE_STALE_SECS as i64 * 1000));
    let polymarket_fresh = last_polymarket_data_age_ms
        .is_some_and(|age| age <= (crate::config::HEALTH_POLYMARKET_STALE_SECS as i64 * 1000));

    let status = if stats.binance_ws_connected
        && stats.poly_ws_connected
        && (stats.last_binance_trade_time.is_none() || stats.last_polymarket_data_time.is_none())
    {
        "starting"
    } else if !stats.binance_ws_connected || !stats.poly_ws_connected {
        "degraded"
    } else if !binance_fresh || !polymarket_fresh {
        "stale"
    } else {
        "ok"
    };

    Json(json!({
        "service": "signal-engine",
        "version": stats.version,
        "status": status,
        "timestamp": timestamp,
        "uptime_secs": stats.uptime_secs,
        "connections": {
            "binance_ws": stats.binance_ws_connected,
            "polymarket_ws": stats.poly_ws_connected,
        },
        "freshness": {
            "last_binance_trade_ms": stats.last_binance_trade_time,
            "last_binance_trade_age_ms": last_binance_trade_age_ms,
            "binance_trade_fresh": binance_fresh,
            "last_polymarket_data_ms": stats.last_polymarket_data_time,
            "last_polymarket_data_age_ms": last_polymarket_data_age_ms,
            "polymarket_data_fresh": polymarket_fresh,
            "last_market_info_ms": stats.last_market_info_time,
            "last_market_info_age_ms": last_market_info_age_ms,
            "thresholds_secs": {
                "binance_trade": crate::config::HEALTH_BINANCE_STALE_SECS,
                "polymarket_data": crate::config::HEALTH_POLYMARKET_STALE_SECS,
            }
        },
        "signal": {
            "method": "drift_estimator_v14_quant_paper",
            "direction": stats.last_signal_direction,
            "confidence": stats.last_signal_confidence,
            "timestamp": stats.last_signal_time,
            "regime": stats.last_regime,
            "path_eff": stats.last_path_eff,
            "confirmation": {
                "count": stats.confirmation_count,
                "direction": stats.confirmation_direction,
                "adaptive_window": stats.last_adaptive_confirm,
                "window_range": format!("{}-{}s", crate::config::MIN_CONFIRM_WINDOW, crate::config::MAX_CONFIRM_WINDOW),
            },
        },
        "calibrated": {
            "mode": stats.calibrated_mode,
            "loaded": stats.calibrated_loaded,
            "artifact_version": stats.calibrated_artifact_version,
            "scores_computed": stats.calibrated_scores_computed,
            "scores_used": stats.calibrated_scores_used,
            "skip_no_ev": stats.skip_calibrated_no_ev,
            "last_prob_up": stats.last_calibrated_prob_up,
            "last_ev_up": stats.last_calibrated_ev_up,
            "last_ev_down": stats.last_calibrated_ev_down,
            "last_direction": stats.last_calibrated_direction,
            "last_ranking_basis": stats.last_ranking_basis,
        },
        "counters": {
            "binance_trades_buffered": stats.binance_trades_buffered,
            "binance_trades_total": stats.binance_trades_total,
            "poly_ticks_received": stats.poly_ticks_received,
            "signals_computed": stats.signals_computed,
            "signals_confirmed": stats.signals_confirmed,
            "entries_fired": stats.entries_fired,
            "skip_penny_contract": stats.skip_penny_contract,
            "skip_price_cap": stats.skip_price_cap,
            "skip_low_edge": stats.skip_low_edge,
            "skip_volume_gate": stats.skip_volume_gate,
            "skip_chop_regime": stats.skip_chop_regime,
            "calibrated_scores_computed": stats.calibrated_scores_computed,
            "calibrated_scores_used": stats.calibrated_scores_used,
            "skip_calibrated_no_ev": stats.skip_calibrated_no_ev,
        },
        "market": {
            "current": stats.current_market,
            "start_ms": stats.market_start_ms,
            "last_btc_price": stats.last_btc_price,
        },
        "config": {
            "entry_confidence": crate::config::entry_confidence(),
            "confirmation_window": format!("adaptive {}-{}s (base {})", crate::config::MIN_CONFIRM_WINDOW, crate::config::MAX_CONFIRM_WINDOW, crate::config::BASE_CONFIRM_WINDOW),
            "min_secs_into_market": crate::config::min_secs_into_market(),
            "max_secs_into_market": crate::config::max_secs_into_market(),
            "max_entry_price": crate::config::max_entry_price(),
            "min_entry_price": crate::config::min_entry_price(),
            "min_edge": crate::config::min_edge(),
            "slippage": crate::config::SLIPPAGE,
            "calibrated_scorer_mode": crate::config::calibrated_scorer_mode(),
            "calibrated_model_path": crate::config::calibrated_model_path(),
            "calibrated_min_ev": crate::config::calibrated_min_ev(),
            "calibrated_score_interval_secs": crate::config::calibrated_score_interval_secs(),
            "weights": {
                "w_drift": crate::config::W_DRIFT,
                "w_scoreboard": crate::config::W_SCOREBOARD,
                "w_ofi_accel": crate::config::W_OFI_ACCEL,
                "whipsaw_weight": crate::config::WHIPSAW_WEIGHT,
            },
            "confidence_calibration": {
                "temperature": crate::config::confidence_temperature(),
                "drift_contrib_cap": crate::config::DRIFT_CONTRIB_CAP,
                "ofi_contrib_cap": crate::config::OFI_CONTRIB_CAP,
                "scoreboard_contrib_cap": crate::config::SCOREBOARD_CONTRIB_CAP,
                "whipsaw_contrib_cap": crate::config::WHIPSAW_CONTRIB_CAP,
            },
            "regime": {
                "trend_threshold": crate::config::REGIME_TREND_THRESHOLD,
                "chop_threshold": crate::config::REGIME_CHOP_THRESHOLD,
                "autocorr_chop": crate::config::REGIME_AUTOCORR_CHOP,
                "lookback": crate::config::REGIME_LOOKBACK,
                "neutral_penalty": crate::config::NEUTRAL_CONF_PENALTY,
            },
        }
    }))
}

/// WebSocket upgrade for downstream clients (execution engine)
pub async fn ws_handler(State(state): State<AppState>, ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(|socket| handle_signal_socket(socket, state))
}

/// Handle a downstream WebSocket connection.
async fn handle_signal_socket(mut ws: WebSocket, state: AppState) {
    info!("🔌 Signal consumer connected");

    let mut rx = state.signal_tx.subscribe();

    let welcome = json!({
        "type": "connected",
        "service": version::SERVICE_SIGNAL_ENGINE,
        "version": version::SIGNAL_VERSION,
        "signal_method": version::SIGNAL_METHOD,
        "stats": {
            "binance_ws_connected": state.get_stats().binance_ws_connected,
            "poly_ws_connected": state.get_stats().poly_ws_connected,
            "current_market": state.get_market().map(|m| m.slug),
        },
        "timestamp": SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    })
    .to_string();

    let _ = ws.send(Message::Text(welcome)).await;

    loop {
        tokio::select! {
            Ok(signal_json) = rx.recv() => {
                if ws.send(Message::Text(signal_json)).await.is_err() {
                    break;
                }
            }
            Some(msg) = ws.recv() => {
                match msg {
                    Ok(Message::Text(text)) => {
                        info!("📨 From downstream: {}", text);
                    }
                    Ok(Message::Close(_)) | Err(_) => break,
                    _ => {}
                }
            }
            else => break,
        }
    }

    info!("🔌 Signal consumer disconnected");
}

/// Root endpoint
pub async fn root_handler() -> impl IntoResponse {
    Json(json!({
        "service": version::SERVICE_SIGNAL_ENGINE,
        "version": version::SIGNAL_VERSION,
        "description": "v14 production signal engine — pure Rust drift + whipsaw + best-candidate selection",
        "signal_method": "weighted drift (57% + OFI 30% + scoreboard 15% + whipsaw residual) with regime gate, adaptive confirmation, and best-candidate entry selection",
        "endpoints": {
            "/health": "Engine stats, signal state, and connection status",
            "/ws": "WebSocket — subscribe to drift signals (for execution engine)",
        }
    }))
}
