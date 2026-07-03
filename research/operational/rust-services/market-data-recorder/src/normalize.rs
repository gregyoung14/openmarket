use serde_json::Value;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::models::{BinanceTick, PolymarketTick};
use crate::services::TokenMapping;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

pub fn parse_f64(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(val) => {
            if let Some(n) = val.as_f64() {
                Some(n)
            } else {
                val.as_str().and_then(|s| s.parse::<f64>().ok())
            }
        }
        None => None,
    }
}

pub fn parse_i64(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(val) => {
            if let Some(n) = val.as_i64() {
                Some(n)
            } else {
                val.as_str().and_then(|s| s.parse::<i64>().ok())
            }
        }
        None => None,
    }
}

pub fn normalize_binance_message(msg: &Value) -> Option<BinanceTick> {
    if msg.get("type").and_then(|v| v.as_str()) != Some("trade") {
        return None;
    }

    let ingest_ts = now_ms();
    let source_ts = parse_i64(msg.get("time")).unwrap_or(ingest_ts);

    Some(BinanceTick {
        source_ts_ms: source_ts,
        ingest_ts_ms: ingest_ts,
        trade_time_ms: source_ts,
        price: parse_f64(msg.get("price")).unwrap_or(0.0),
        volume: parse_f64(msg.get("volume")).unwrap_or(0.0),
        raw_json: msg.to_string(),
    })
}

pub fn normalize_polymarket_message(msg: &Value, mapping: &TokenMapping) -> Vec<PolymarketTick> {
    let ingest_ts = now_ms();
    let msg_type = msg
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");

    if msg_type == "price_change" {
        let asset_id = msg
            .get("asset_id")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        if asset_id.is_empty() {
            return vec![];
        }

        let (side_label, market_slug) = resolve_asset(&asset_id, mapping);
        let source_ts = parse_i64(msg.get("timestamp")).unwrap_or(ingest_ts);

        if side_label == "UNKNOWN" {
            tracing::warn!(
                "UNKNOWN asset_id {} in market {:?} — asset_map miss, check token mapping",
                &asset_id[..20.min(asset_id.len())],
                market_slug
            );
        }

        return vec![PolymarketTick {
            source_ts_ms: source_ts,
            ingest_ts_ms: ingest_ts,
            market_slug,
            asset_id,
            side_label,
            event_type: "price_change".to_string(),
            price: parse_f64(msg.get("price")),
            best_bid: parse_f64(msg.get("best_bid")).or_else(|| parse_f64(msg.get("price"))),
            best_ask: parse_f64(msg.get("best_ask")),
            size: parse_f64(msg.get("size")),
            raw_json: msg.to_string(),
        }];
    }

    if msg_type == "book" {
        let asset_id = msg
            .get("asset_id")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        if asset_id.is_empty() {
            return vec![];
        }

        let bids = msg.get("bids").and_then(|v| v.as_array());
        let asks = msg.get("asks").and_then(|v| v.as_array());

        let best_bid = bids.and_then(|arr| {
            arr.iter()
                .filter_map(|it| it.as_array().and_then(|pair| parse_f64(pair.first())))
                .max_by(|a, b| a.partial_cmp(b).unwrap())
        });

        let best_ask = asks.and_then(|arr| {
            arr.iter()
                .filter_map(|it| it.as_array().and_then(|pair| parse_f64(pair.first())))
                .filter(|x| *x > 0.0)
                .min_by(|a, b| a.partial_cmp(b).unwrap())
        });

        let source_ts = parse_i64(msg.get("timestamp")).unwrap_or(ingest_ts);
        let (side_label, market_slug) = resolve_asset(&asset_id, mapping);

        return vec![PolymarketTick {
            source_ts_ms: source_ts,
            ingest_ts_ms: ingest_ts,
            market_slug,
            asset_id: asset_id.clone(),
            side_label,
            event_type: "book".to_string(),
            price: None,
            best_bid,
            best_ask,
            size: None,
            raw_json: msg.to_string(),
        }];
    }

    vec![]
}

/// Resolves an asset_id to its side label ("UP"/"DOWN") and correct market slug.
///
/// Priority:
/// 1. `asset_map` — persistent across all market transitions
/// 2. Current `up_token_id` / `down_token_id` — the active market
/// 3. "UNKNOWN" fallback (should only happen if asset_id is truly novel)
pub fn resolve_asset(asset_id: &str, mapping: &TokenMapping) -> (String, Option<String>) {
    // 1. Check persistent asset_map
    if let Some(info) = mapping.asset_map.get(asset_id) {
        return (info.side.clone(), Some(info.market_slug.clone()));
    }

    // 2. Check current market tokens
    if mapping.up_token_id.as_deref() == Some(asset_id) {
        return ("UP".to_string(), mapping.market_slug.clone());
    }
    if mapping.down_token_id.as_deref() == Some(asset_id) {
        return ("DOWN".to_string(), mapping.market_slug.clone());
    }

    // 3. Unknown — will be logged by caller
    ("UNKNOWN".to_string(), mapping.market_slug.clone())
}
