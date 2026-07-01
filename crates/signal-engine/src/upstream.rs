//! Upstream WebSocket connectors — subscribes to Binance & Polymarket
//! Rust services and feeds the trade buffer / market state.

use crate::config;
use crate::models::{BinanceTrade, MarketInfo};
use crate::state::AppState;
use btc_common::version;
use futures::stream::StreamExt;
use serde_json::Value;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::time::sleep;
use tracing::{error, info, warn};

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

fn parse_f64_value(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.parse::<f64>().ok(),
        _ => None,
    }
    .filter(|v| v.is_finite() && *v > 0.0)
}

fn extract_message_type(data: &Value) -> Option<&str> {
    data.get("event_type")
        .or_else(|| data.get("type"))
        .and_then(|v| v.as_str())
}

fn extract_string_field(data: &Value, key: &str) -> Option<String> {
    data.get(key)
        .and_then(|v| v.as_str())
        .map(ToString::to_string)
}

fn parse_string_vec_field(data: &Value, key: &str) -> Vec<String> {
    let Some(value) = data.get(key) else {
        return Vec::new();
    };

    if let Some(arr) = value.as_array() {
        return arr
            .iter()
            .filter_map(|v| v.as_str().map(ToString::to_string))
            .collect();
    }

    value
        .as_str()
        .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
        .unwrap_or_default()
}

fn extract_token_ids(data: &Value) -> Vec<String> {
    let token_ids = parse_string_vec_field(data, "token_ids");
    if !token_ids.is_empty() {
        return token_ids;
    }

    let assets_ids = parse_string_vec_field(data, "assets_ids");
    if !assets_ids.is_empty() {
        return assets_ids;
    }

    parse_string_vec_field(data, "clobTokenIds")
}

fn parse_book_best_price(levels: &Value, want_bid: bool) -> Option<f64> {
    match levels {
        Value::Array(rows) => {
            let mut best: Option<f64> = None;
            for row in rows {
                let price = match row {
                    Value::Array(values) => values.first().and_then(parse_f64_value),
                    Value::Object(map) => map.get("price").and_then(parse_f64_value),
                    _ => None,
                };

                let Some(price) = price else {
                    continue;
                };

                best = match best {
                    None => Some(price),
                    Some(curr) if want_bid && price > curr => Some(price),
                    Some(curr) if !want_bid && price < curr => Some(price),
                    Some(curr) => Some(curr),
                };
            }
            best
        }
        Value::Object(map) => {
            let mut best: Option<f64> = None;
            for key in map.keys() {
                let Ok(price) = key.parse::<f64>() else {
                    continue;
                };

                if !(price.is_finite() && price > 0.0) {
                    continue;
                }

                best = match best {
                    None => Some(price),
                    Some(curr) if want_bid && price > curr => Some(price),
                    Some(curr) if !want_bid && price < curr => Some(price),
                    Some(curr) => Some(curr),
                };
            }
            best
        }
        _ => None,
    }
}

fn update_poly_price_from_change(
    state: &AppState,
    asset_id: &str,
    fallback_side: Option<String>,
    best_bid: f64,
    best_ask: f64,
) {
    let current_time_ms = now_ms();
    let side = state
        .get_side_for_token(asset_id)
        .or(fallback_side)
        .filter(|side| side == "UP" || side == "DOWN");
    let Some(side) = side else {
        return;
    };

    state.update_poly_price(&side, best_bid, best_ask);
    state.update_stats(|s| {
        s.poly_ticks_received += 1;
        s.last_polymarket_data_time = Some(current_time_ms);
    });
}

fn handle_polymarket_data(state: &AppState, data: &Value) {
    let current_time_ms = now_ms();
    let msg_type = extract_message_type(data).unwrap_or("");

    match msg_type {
        "market_info" | "new_market" => {
            if let Some(slug) = extract_string_field(data, "slug") {
                let epoch_s: i64 = slug
                    .split('-')
                    .next_back()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                let start_ms = epoch_s * 1000;
                let end_ms = start_ms + (config::MARKET_DURATION_SECS as i64 * 1000);

                let ids = extract_token_ids(data);
                if ids.len() >= 2 {
                    state.set_token_sides(&ids);
                }

                let existing_market = state.get_market();
                let up_price = data
                    .get("up_price")
                    .and_then(parse_f64_value)
                    .or_else(|| existing_market.as_ref().map(|m| m.up_price))
                    .unwrap_or(0.5);
                let down_price = data
                    .get("down_price")
                    .and_then(parse_f64_value)
                    .or_else(|| existing_market.as_ref().map(|m| m.down_price))
                    .unwrap_or(0.5);

                let market = MarketInfo {
                    slug: slug.clone(),
                    start_ms,
                    end_ms,
                    up_price,
                    down_price,
                    up_best_ask: up_price,
                    down_best_ask: down_price,
                    up_best_bid: up_price,
                    down_best_bid: down_price,
                };

                let should_reset_market =
                    state.get_market().map(|m| m.slug != slug).unwrap_or(true);

                if should_reset_market {
                    info!(
                        "📈 New market: {} (UP={:.2} DOWN={:.2}) start={}",
                        slug, up_price, down_price, start_ms
                    );
                    state.new_market(market);
                }

                let _ = state.signal_tx.send(
                    serde_json::json!({
                        "type": "market_info",
                        "slug": slug,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "up_price": up_price,
                        "down_price": down_price,
                        "token_ids": ids,
                        "assets_ids": extract_token_ids(data),
                        "version": version::SIGNAL_VERSION,
                    })
                    .to_string(),
                );

                state.update_stats(|s| {
                    s.last_polymarket_data_time = Some(current_time_ms);
                    s.last_market_info_time = Some(current_time_ms);
                });
            }
        }
        "price_change" => {
            if let Some(changes) = data.get("price_changes").and_then(|v| v.as_array()) {
                for change in changes {
                    let Some(asset_id) = change.get("asset_id").and_then(|v| v.as_str()) else {
                        continue;
                    };

                    let fallback_side = change
                        .get("market_side")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_uppercase());
                    let best_bid = change
                        .get("best_bid")
                        .and_then(parse_f64_value)
                        .unwrap_or(0.0);
                    let best_ask = change
                        .get("best_ask")
                        .and_then(parse_f64_value)
                        .unwrap_or(0.0);

                    update_poly_price_from_change(
                        state,
                        asset_id,
                        fallback_side,
                        best_bid,
                        best_ask,
                    );
                }
            } else if let Some(asset_id) = data.get("asset_id").and_then(|v| v.as_str()) {
                let fallback_side = data
                    .get("market_side")
                    .or_else(|| data.get("side"))
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_uppercase());
                let best_bid = data
                    .get("best_bid")
                    .and_then(parse_f64_value)
                    .unwrap_or(0.0);
                let best_ask = data
                    .get("best_ask")
                    .and_then(parse_f64_value)
                    .unwrap_or(0.0);

                update_poly_price_from_change(state, asset_id, fallback_side, best_bid, best_ask);
            }
        }
        "trade" | "best_bid_ask" => {
            let Some(asset_id) = data.get("asset_id").and_then(|v| v.as_str()) else {
                return;
            };

            let fallback_side = data
                .get("market_side")
                .and_then(|v| v.as_str())
                .map(|s| s.to_uppercase());
            let best_bid = data
                .get("best_bid")
                .and_then(parse_f64_value)
                .unwrap_or(0.0);
            let best_ask = data
                .get("best_ask")
                .and_then(parse_f64_value)
                .unwrap_or(0.0);

            update_poly_price_from_change(state, asset_id, fallback_side, best_bid, best_ask);
        }
        "book" => {
            let Some(asset_id) = data.get("asset_id").and_then(|v| v.as_str()) else {
                return;
            };

            let best_bid = data
                .get("bids")
                .and_then(|v| parse_book_best_price(v, true))
                .unwrap_or(0.0);

            let best_ask = data
                .get("asks")
                .and_then(|v| parse_book_best_price(v, false))
                .unwrap_or(0.0);

            let fallback_side = data
                .get("market_side")
                .or_else(|| data.get("side"))
                .and_then(|v| v.as_str())
                .map(|s| s.to_uppercase());

            update_poly_price_from_change(state, asset_id, fallback_side, best_bid, best_ask);
        }
        "connected" => {
            info!("Polymarket upstream acknowledged connection");
        }
        _ => {}
    }
}

fn extract_bool_field(data: &Value, key: &str) -> Option<bool> {
    let value = data.get(key)?;
    if let Some(flag) = value.as_bool() {
        return Some(flag);
    }
    if let Some(number) = value.as_i64() {
        return Some(number != 0);
    }
    if let Some(text) = value.as_str() {
        return match text.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "yes" | "y" => Some(true),
            "0" | "false" | "f" | "no" | "n" => Some(false),
            _ => None,
        };
    }
    None
}

// ================================================================
// Binance WS upstream
// ================================================================

pub async fn binance_upstream_task(state: AppState) {
    let mut attempt = 0u32;

    loop {
        info!(
            "Connecting to Binance WS upstream at {} (attempt {})...",
            config::BINANCE_WS_URL,
            attempt + 1
        );

        match tokio_tungstenite::connect_async(config::BINANCE_WS_URL).await {
            Ok((ws_stream, _)) => {
                info!("✅ Connected to Binance upstream WS");
                state.update_stats(|s| s.binance_ws_connected = true);
                attempt = 0;

                let (_, mut read) = ws_stream.split();

                while let Some(msg) = read.next().await {
                    match msg {
                        Ok(tokio_tungstenite::tungstenite::Message::Text(text)) => {
                            if let Ok(data) = serde_json::from_str::<Value>(&text) {
                                let msg_type =
                                    data.get("type").and_then(|v| v.as_str()).unwrap_or("");

                                if msg_type == "trade" {
                                    let trade_time = data
                                        .get("time")
                                        .and_then(|v| v.as_i64())
                                        .or_else(|| data.get("T").and_then(|v| v.as_i64()))
                                        .or_else(|| data.get("timestamp").and_then(|v| v.as_i64()))
                                        .unwrap_or(0);

                                    let price = data
                                        .get("price")
                                        .and_then(|v| v.as_f64())
                                        .or_else(|| data.get("p").and_then(|v| v.as_f64()))
                                        .unwrap_or(0.0);

                                    let quantity = data
                                        .get("volume")
                                        .and_then(|v| v.as_f64())
                                        .or_else(|| data.get("quantity").and_then(|v| v.as_f64()))
                                        .or_else(|| data.get("q").and_then(|v| v.as_f64()))
                                        .unwrap_or(0.001);

                                    let is_buyer_maker = extract_bool_field(&data, "m")
                                        .or_else(|| extract_bool_field(&data, "is_buyer_maker"))
                                        .unwrap_or(false);

                                    let trade = BinanceTrade {
                                        trade_time_ms: trade_time,
                                        price,
                                        quantity,
                                        is_buyer_maker,
                                    };

                                    state.push_trade(trade);
                                }
                            }
                        }
                        Ok(tokio_tungstenite::tungstenite::Message::Close(_)) => {
                            info!("Binance upstream closed");
                            break;
                        }
                        Err(e) => {
                            error!("Binance upstream error: {}", e);
                            break;
                        }
                        _ => {}
                    }
                }

                state.update_stats(|s| s.binance_ws_connected = false);
                warn!("Binance upstream WS disconnected");
            }
            Err(e) => {
                if attempt.is_multiple_of(10) {
                    warn!(
                        "Cannot connect to Binance upstream: {} (attempt {})",
                        e,
                        attempt + 1
                    );
                }
                attempt += 1;
            }
        }

        let exp = attempt.min(8);
        let delay_ms = config::RECONNECT_BASE_DELAY_MS * 2u64.pow(exp);
        let delay = Duration::from_millis(delay_ms.min(config::MAX_RECONNECT_DELAY_SECS * 1000));
        sleep(delay).await;
    }
}

// ================================================================
// Polymarket WS upstream
// ================================================================

pub async fn polymarket_upstream_task(state: AppState) {
    let mut attempt = 0u32;

    loop {
        info!(
            "Connecting to Polymarket WS upstream at {} (attempt {})...",
            config::POLYMARKET_WS_URL,
            attempt + 1
        );

        match tokio_tungstenite::connect_async(config::POLYMARKET_WS_URL).await {
            Ok((ws_stream, _)) => {
                info!("✅ Connected to Polymarket upstream WS");
                state.update_stats(|s| s.poly_ws_connected = true);
                attempt = 0;

                let (_, mut read) = ws_stream.split();

                while let Some(msg) = read.next().await {
                    match msg {
                        Ok(tokio_tungstenite::tungstenite::Message::Text(text)) => {
                            if let Ok(data) = serde_json::from_str::<Value>(&text) {
                                handle_polymarket_data(&state, &data);
                            }
                        }
                        Ok(tokio_tungstenite::tungstenite::Message::Close(_)) => {
                            info!("Polymarket upstream closed");
                            break;
                        }
                        Err(e) => {
                            error!("Polymarket upstream error: {}", e);
                            break;
                        }
                        _ => {}
                    }
                }

                state.update_stats(|s| s.poly_ws_connected = false);
                warn!("Polymarket upstream WS disconnected");
            }
            Err(e) => {
                if attempt.is_multiple_of(10) {
                    warn!(
                        "Cannot connect to Polymarket upstream: {} (attempt {})",
                        e,
                        attempt + 1
                    );
                }
                attempt += 1;
            }
        }

        let exp = attempt.min(8);
        let delay_ms = config::RECONNECT_BASE_DELAY_MS * 2u64.pow(exp);
        let delay = Duration::from_millis(delay_ms.min(config::MAX_RECONNECT_DELAY_SECS * 1000));
        sleep(delay).await;
    }
}

#[cfg(test)]
mod tests {
    use super::{extract_bool_field, handle_polymarket_data};
    use crate::state::AppState;
    use serde_json::{json, Value};
    use tokio::sync::broadcast;

    #[test]
    fn bool_field_variants() {
        let data = json!({"a": true, "b": 1, "c": "true", "d": false, "e": 0, "f": "false"});
        assert_eq!(extract_bool_field(&data, "a"), Some(true));
        assert_eq!(extract_bool_field(&data, "b"), Some(true));
        assert_eq!(extract_bool_field(&data, "c"), Some(true));
        assert_eq!(extract_bool_field(&data, "d"), Some(false));
        assert_eq!(extract_bool_field(&data, "e"), Some(false));
        assert_eq!(extract_bool_field(&data, "f"), Some(false));
    }

    #[test]
    fn new_market_updates_state_and_broadcasts_normalized_market_info() {
        let (tx, _) = broadcast::channel(16);
        let state = AppState::new(tx);
        let mut rx = state.signal_tx.subscribe();
        let payload: Value = json!({
            "event_type": "new_market",
            "slug": "btc-updown-15m-1771695900",
            "assets_ids": ["up_token_123", "down_token_456"],
            "question": "Will BTC go up?"
        });

        handle_polymarket_data(&state, &payload);

        let market = state.get_market().expect("market should be set");
        assert_eq!(market.slug, "btc-updown-15m-1771695900");
        assert_eq!(
            state.get_side_for_token("up_token_123").as_deref(),
            Some("UP")
        );
        assert_eq!(
            state.get_side_for_token("down_token_456").as_deref(),
            Some("DOWN")
        );

        let broadcast = rx.try_recv().expect("expected normalized market_info");
        let json: Value = serde_json::from_str(&broadcast).unwrap();
        assert_eq!(
            json.get("type").and_then(|v| v.as_str()),
            Some("market_info")
        );
        assert_eq!(
            json.get("slug").and_then(|v| v.as_str()),
            Some("btc-updown-15m-1771695900")
        );
        assert_eq!(
            json.get("token_ids")
                .and_then(|v| v.as_array())
                .map(|a| a.len()),
            Some(2)
        );
    }

    #[test]
    fn official_price_change_array_updates_both_sides() {
        let (tx, _) = broadcast::channel(16);
        let state = AppState::new(tx);
        state.new_market(crate::models::MarketInfo {
            slug: "btc-updown-15m-1771695900".to_string(),
            start_ms: 1771695900_i64 * 1000,
            end_ms: (1771695900_i64 + crate::config::MARKET_DURATION_SECS as i64) * 1000,
            up_price: 0.5,
            down_price: 0.5,
            up_best_ask: 0.5,
            down_best_ask: 0.5,
            up_best_bid: 0.5,
            down_best_bid: 0.5,
        });
        state.set_token_sides(&["up_token_123".to_string(), "down_token_456".to_string()]);

        let payload: Value = json!({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "up_token_123", "best_bid": "0.44", "best_ask": "0.45", "side": "SELL"},
                {"asset_id": "down_token_456", "best_bid": "0.54", "best_ask": "0.55", "side": "BUY"}
            ]
        });

        handle_polymarket_data(&state, &payload);

        let market = state.get_market().expect("market should remain set");
        assert_eq!(market.up_best_bid, 0.44);
        assert_eq!(market.up_best_ask, 0.45);
        assert_eq!(market.down_best_bid, 0.54);
        assert_eq!(market.down_best_ask, 0.55);

        let stats = state.get_stats();
        assert_eq!(stats.poly_ticks_received, 2);
    }
}
