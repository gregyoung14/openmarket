use futures::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{error, info, warn};

use crate::models::PolymarketMessage;

use crate::config;
use crate::models::SignalMessage;

fn parse_string_vec_field(value: &serde_json::Value, key: &str) -> Vec<String> {
    let Some(raw) = value.get(key) else {
        return Vec::new();
    };

    if let Some(arr) = raw.as_array() {
        return arr
            .iter()
            .filter_map(|v| v.as_str().map(ToString::to_string))
            .collect();
    }

    raw.as_str()
        .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
        .unwrap_or_default()
}

fn parse_market_side(value: Option<&serde_json::Value>) -> Option<String> {
    value
        .and_then(|v| v.as_str())
        .map(|s| s.to_uppercase())
        .filter(|s| s == "UP" || s == "DOWN")
}

fn parse_price_value(value: &serde_json::Value) -> Option<f64> {
    match value {
        serde_json::Value::Number(n) => n.as_f64(),
        serde_json::Value::String(s) => s.parse::<f64>().ok(),
        _ => None,
    }
    .filter(|v| v.is_finite() && *v > 0.0)
}

fn parse_book_side(side: &serde_json::Value, want_bid: bool) -> Option<f64> {
    match side {
        serde_json::Value::Object(map) => {
            let mut best: Option<f64> = None;
            for price_raw in map.keys() {
                let Ok(price) = price_raw.parse::<f64>() else {
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
        serde_json::Value::Array(rows) => {
            let mut best: Option<f64> = None;
            for row in rows {
                let price = match row {
                    serde_json::Value::Object(m) => m.get("price").and_then(parse_price_value),
                    serde_json::Value::Array(a) => a.first().and_then(parse_price_value),
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
        _ => None,
    }
}

fn parse_polymarket_value(value: serde_json::Value) -> Result<Vec<PolymarketMessage>, String> {
    let event_type = value
        .get("type")
        .or_else(|| value.get("event_type"))
        .and_then(|v| v.as_str())
        .unwrap_or("");

    match event_type {
        "book" => {
            let asset_id = value
                .get("asset_id")
                .and_then(|v| v.as_str())
                .map(ToString::to_string);
            let best_bid = value.get("bids").and_then(|v| parse_book_side(v, true));
            let best_ask = value.get("asks").and_then(|v| parse_book_side(v, false));
            let side = value
                .get("side")
                .and_then(|v| v.as_str())
                .map(|s| s.to_uppercase());
            let market_side = parse_market_side(value.get("market_side"));

            Ok(vec![PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                price: None,
                token_id: None,
                asset_id,
                side,
                market_side,
            }])
        }
        "price_change" => {
            if let Some(changes) = value.get("price_changes").and_then(|v| v.as_array()) {
                let messages = changes
                    .iter()
                    .filter_map(|change| {
                        let asset_id = change
                            .get("asset_id")
                            .and_then(|v| v.as_str())
                            .map(ToString::to_string)?;
                        let price = change.get("price").and_then(parse_price_value);
                        let side = change
                            .get("side")
                            .and_then(|v| v.as_str())
                            .map(|s| s.to_uppercase());
                        let market_side = parse_market_side(change.get("market_side"))
                            .or_else(|| parse_market_side(value.get("market_side")));

                        let best_bid = change.get("best_bid").and_then(parse_price_value).or_else(
                            || match side.as_deref() {
                                Some("BUY") | Some("BID") => price,
                                _ => None,
                            },
                        );
                        let best_ask = change.get("best_ask").and_then(parse_price_value).or_else(
                            || match side.as_deref() {
                                Some("SELL") | Some("ASK") => price,
                                _ => None,
                            },
                        );

                        Some(PolymarketMessage::PriceChange {
                            best_bid,
                            best_ask,
                            price,
                            token_id: None,
                            asset_id: Some(asset_id),
                            side,
                            market_side,
                        })
                    })
                    .collect::<Vec<_>>();

                if messages.is_empty() {
                    Err("price_change message contained no valid price_changes".to_string())
                } else {
                    Ok(messages)
                }
            } else {
                let asset_id = value
                    .get("asset_id")
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string);
                let price = value.get("price").and_then(parse_price_value);
                let side = value
                    .get("side")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_uppercase());
                let best_bid = value
                    .get("best_bid")
                    .and_then(parse_price_value)
                    .or_else(|| match side.as_deref() {
                        Some("BUY") | Some("BID") => price,
                        _ => None,
                    });
                let best_ask = value
                    .get("best_ask")
                    .and_then(parse_price_value)
                    .or_else(|| match side.as_deref() {
                        Some("SELL") | Some("ASK") => price,
                        _ => None,
                    });
                let market_side = parse_market_side(value.get("market_side"));

                Ok(vec![PolymarketMessage::PriceChange {
                    best_bid,
                    best_ask,
                    price,
                    token_id: None,
                    asset_id,
                    side,
                    market_side,
                }])
            }
        }
        "market_info" | "new_market" => {
            let token_ids = {
                let direct = parse_string_vec_field(&value, "token_ids");
                if direct.is_empty() {
                    parse_string_vec_field(&value, "assets_ids")
                } else {
                    direct
                }
            };

            Ok(vec![PolymarketMessage::MarketInfo {
                slug: value
                    .get("slug")
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string),
                question: value
                    .get("question")
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string),
                token_ids: if token_ids.is_empty() {
                    None
                } else {
                    Some(token_ids)
                },
                end_date: value
                    .get("end_date")
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string),
            }])
        }
        _ => serde_json::from_value::<PolymarketMessage>(value)
            .map(|msg| vec![msg])
            .map_err(|e| format!("typed parse error: {e}")),
    }
}

fn parse_polymarket_message(text: &str) -> Result<Vec<PolymarketMessage>, String> {
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|e| format!("json parse error: {e}"))?;

    if let Some(items) = value.as_array() {
        let mut out = Vec::new();
        for item in items {
            out.extend(parse_polymarket_value(item.clone())?);
        }
        return Ok(out);
    }

    parse_polymarket_value(value)
}

/// Connects to the signal-engine WS and forwards parsed messages to the channel.
/// Auto-reconnects with exponential backoff.
pub async fn run(tx: mpsc::UnboundedSender<SignalMessage>) {
    let mut delay = config::RECONNECT_BASE_DELAY;

    loop {
        info!(
            url = config::SIGNAL_ENGINE_WS,
            "Connecting to signal engine"
        );

        match connect_async(config::SIGNAL_ENGINE_WS).await {
            Ok((ws_stream, _)) => {
                info!("Connected to signal engine");
                delay = config::RECONNECT_BASE_DELAY; // reset backoff

                let (mut _write, mut read) = ws_stream.split();

                while let Some(msg_result) = read.next().await {
                    match msg_result {
                        Ok(Message::Text(text)) => {
                            match serde_json::from_str::<SignalMessage>(&text) {
                                Ok(signal) => {
                                    if tx.send(signal).is_err() {
                                        error!("Signal channel closed, stopping consumer");
                                        return;
                                    }
                                }
                                Err(e) => {
                                    warn!(error = %e, text = %text, "Failed to parse signal message");
                                }
                            }
                        }
                        Ok(Message::Ping(data)) => {
                            // Respond with pong (tungstenite handles this automatically usually)
                            let _ = _write.send(Message::Pong(data)).await;
                        }
                        Ok(Message::Close(_)) => {
                            warn!("Signal engine WS closed");
                            break;
                        }
                        Err(e) => {
                            error!(error = %e, "Signal engine WS error");
                            break;
                        }
                        _ => {}
                    }
                }
            }
            Err(e) => {
                error!(error = %e, "Failed to connect to signal engine");
            }
        }

        warn!(
            delay_ms = delay.as_millis(),
            "Reconnecting to signal engine"
        );
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(config::MAX_RECONNECT_DELAY);
    }
}

/// Connects to the polymarket-websocket service for live bid/ask prices.
/// Parsed messages forwarded via channel.
pub async fn run_price_feed(tx: mpsc::UnboundedSender<crate::models::PolymarketMessage>) {
    let mut delay = config::RECONNECT_BASE_DELAY;

    loop {
        info!(
            url = config::POLYMARKET_WS,
            "Connecting to polymarket price feed"
        );

        match connect_async(config::POLYMARKET_WS).await {
            Ok((ws_stream, _)) => {
                info!("Connected to polymarket price feed");
                delay = config::RECONNECT_BASE_DELAY;

                let (mut _write, mut read) = ws_stream.split();

                while let Some(msg_result) = read.next().await {
                    match msg_result {
                        Ok(Message::Text(text)) => match parse_polymarket_message(&text) {
                            Ok(messages) => {
                                for msg in messages {
                                    if tx.send(msg).is_err() {
                                        error!("Price channel closed, stopping");
                                        return;
                                    }
                                }
                            }
                            Err(e) => {
                                warn!(error = %e, "Failed to parse polymarket message");
                            }
                        },
                        Ok(Message::Ping(data)) => {
                            let _ = _write.send(Message::Pong(data)).await;
                        }
                        Ok(Message::Close(_)) => {
                            warn!("Polymarket WS closed");
                            break;
                        }
                        Err(e) => {
                            error!(error = %e, "Polymarket WS error");
                            break;
                        }
                        _ => {}
                    }
                }
            }
            Err(e) => {
                error!(error = %e, "Failed to connect to polymarket WS");
            }
        }

        warn!(
            delay_ms = delay.as_millis(),
            "Reconnecting to polymarket WS"
        );
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(config::MAX_RECONNECT_DELAY);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::PolymarketMessage;

    #[test]
    fn parses_book_with_market_side_and_raw_side() {
        let json = r#"{
            "type": "book",
            "asset_id": "asset_up",
            "side": "buy",
            "market_side": "UP",
            "bids": {"0.44":"10","0.45":"5"},
            "asks": {"0.46":"3","0.47":"4"}
        }"#;

        let msg = parse_polymarket_message(json).expect("book should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                side,
                market_side,
                ..
            } => {
                assert_eq!(best_bid, Some(0.45));
                assert_eq!(best_ask, Some(0.46));
                assert_eq!(side.as_deref(), Some("BUY"));
                assert_eq!(market_side.as_deref(), Some("UP"));
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    #[test]
    fn parses_price_change_preserving_raw_side_and_market_side() {
        let json = r#"{
            "type": "price_change",
            "asset_id": "asset_down",
            "side": "sell",
            "market_side": "DOWN",
            "price": "0.39"
        }"#;

        let msg = parse_polymarket_message(json).expect("price_change should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                side,
                market_side,
                ..
            } => {
                assert_eq!(best_bid, None);
                assert_eq!(best_ask, Some(0.39));
                assert_eq!(side.as_deref(), Some("SELL"));
                assert_eq!(market_side.as_deref(), Some("DOWN"));
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    #[test]
    fn rejects_invalid_market_side_values() {
        let json = r#"{
            "type": "price_change",
            "asset_id": "asset_unknown",
            "side": "buy",
            "market_side": "YES",
            "price": 0.51
        }"#;

        let msg = parse_polymarket_message(json).expect("price_change should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                side, market_side, ..
            } => {
                assert_eq!(side.as_deref(), Some("BUY"));
                assert_eq!(market_side, None);
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    // ── Real wire-format tests (matching polymarket-websocket output) ──

    /// Book message exactly as polymarket-websocket emits it:
    /// - "type" (not "event_type")
    /// - bids/asks as array-of-arrays [[price, size], ...]
    /// - "side" contains resolved UP/DOWN (not "market_side" for books)
    #[test]
    fn parses_real_wire_book_message() {
        let json = r#"{
            "type": "book",
            "asset_id": "0x1234567890abcdef",
            "bids": [[0.44, 10.0], [0.43, 20.0], [0.42, 50.0]],
            "asks": [[0.46, 5.0], [0.47, 8.0], [0.48, 15.0]],
            "side": "UP",
            "timestamp": 1771696010648
        }"#;

        let msg = parse_polymarket_message(json).expect("real wire book should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                side,
                market_side,
                asset_id,
                ..
            } => {
                assert_eq!(best_bid, Some(0.44), "best bid from array-of-arrays");
                assert_eq!(best_ask, Some(0.46), "best ask from array-of-arrays");
                assert_eq!(asset_id.as_deref(), Some("0x1234567890abcdef"));
                // For book messages, "side" holds the resolved UP/DOWN
                assert_eq!(side.as_deref(), Some("UP"));
                // "market_side" is not present in book messages from polymarket-websocket
                // but the explicit parser reads both fields
                let _ = market_side; // may or may not be set depending on wire format
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    /// Price change message exactly as polymarket-websocket emits it:
    /// - "type": "price_change"
    /// - "side": raw order side (BUY/SELL)
    /// - "market_side": resolved UP/DOWN
    #[test]
    fn parses_real_wire_price_change_message() {
        let json = r#"{
            "type": "price_change",
            "asset_id": "0xabcdef1234567890",
            "price": 0.46,
            "size": 5.0,
            "side": "SELL",
            "market_side": "UP",
            "best_bid": 0.45,
            "best_ask": 0.46,
            "timestamp": 1771696010648
        }"#;

        let msg = parse_polymarket_message(json).expect("real wire price_change should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                price,
                side,
                market_side,
                asset_id,
                ..
            } => {
                assert_eq!(
                    best_bid,
                    Some(0.45),
                    "best_bid should preserve explicit field"
                );
                assert_eq!(best_ask, Some(0.46), "SELL price → best_ask");
                assert_eq!(price, Some(0.46));
                assert_eq!(side.as_deref(), Some("SELL"));
                assert_eq!(market_side.as_deref(), Some("UP"));
                assert_eq!(asset_id.as_deref(), Some("0xabcdef1234567890"));
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    /// Verify that "type" triggers explicit parsing (not serde fallback).
    /// Book messages should be normalized to PriceChange (not Book variant).
    #[test]
    fn book_messages_normalized_to_price_change_not_book_variant() {
        let json = r#"{
            "type": "book",
            "asset_id": "0xtest",
            "bids": [[0.50, 100.0]],
            "asks": [[0.51, 100.0]],
            "side": "DOWN"
        }"#;

        let msg = parse_polymarket_message(json).expect("should parse");
        let first = msg.first().cloned().expect("expected parsed message");
        // The explicit parser converts book → PriceChange (not Book variant)
        assert!(
            matches!(first, PolymarketMessage::PriceChange { .. }),
            "Book messages should be normalized to PriceChange, got {msg:?}"
        );
    }

    /// Verify BUY side on price_change sets best_bid correctly.
    #[test]
    fn price_change_buy_side_sets_best_bid() {
        let json = r#"{
            "type": "price_change",
            "asset_id": "0xtest",
            "price": 0.44,
            "side": "BUY",
            "market_side": "DOWN"
        }"#;

        let msg = parse_polymarket_message(json).expect("should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                side,
                market_side,
                ..
            } => {
                assert_eq!(best_bid, Some(0.44), "BUY price should set best_bid");
                assert_eq!(best_ask, None, "BUY price should not set best_ask");
                assert_eq!(side.as_deref(), Some("BUY"));
                assert_eq!(market_side.as_deref(), Some("DOWN"));
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    /// Book with object-format bids/asks (upstream Polymarket format)
    #[test]
    fn parses_book_with_object_format_levels() {
        let json = r#"{
            "type": "book",
            "asset_id": "0xobj_test",
            "bids": {"0.44":"10","0.45":"5","0.43":"20"},
            "asks": {"0.47":"4","0.46":"3","0.48":"8"},
            "side": "UP"
        }"#;

        let msg = parse_polymarket_message(json).expect("object-format book should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::PriceChange {
                best_bid, best_ask, ..
            } => {
                assert_eq!(best_bid, Some(0.45), "best bid from object keys");
                assert_eq!(best_ask, Some(0.46), "best ask from object keys");
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    /// Connected message should parse via serde fallback (no explicit handler)
    #[test]
    fn connected_message_parses_via_serde() {
        let json = r#"{
            "type": "connected",
            "service": "polymarket-websocket",
            "timestamp": 1771696000000
        }"#;

        let msg = parse_polymarket_message(json).expect("connected should parse");
        assert!(
            matches!(
                msg.into_iter().next().unwrap(),
                PolymarketMessage::Connected { .. }
            ),
            "Connected message should parse via serde fallback"
        );
    }

    /// MarketInfo message parses correctly with token_ids
    #[test]
    fn market_info_message_parses_via_serde() {
        let json = r#"{
            "type": "market_info",
            "slug": "btc-updown-15m-1771695900",
            "question": "Will BTC go up?",
            "token_ids": ["token_up_123", "token_down_456"]
        }"#;

        let msg = parse_polymarket_message(json).expect("market_info should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::MarketInfo {
                slug, token_ids, ..
            } => {
                assert_eq!(slug.as_deref(), Some("btc-updown-15m-1771695900"));
                let ids = token_ids.unwrap();
                assert_eq!(ids.len(), 2);
                assert_eq!(ids[0], "token_up_123");
            }
            other => panic!("expected MarketInfo, got {other:?}"),
        }
    }

    /// Unknown type should parse gracefully
    #[test]
    fn unknown_type_parses_via_serde_fallback() {
        let json = r#"{"type": "some_future_type", "data": 42}"#;
        let msg = parse_polymarket_message(json).expect("unknown type should parse");
        assert!(
            matches!(msg.into_iter().next().unwrap(), PolymarketMessage::Unknown),
            "Unknown type should become Unknown variant"
        );
    }

    #[test]
    fn official_price_change_array_expands_to_multiple_messages() {
        let json = r#"{
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "up_token", "price": "0.48", "best_bid": "0.47", "best_ask": "0.48", "side": "SELL"},
                {"asset_id": "down_token", "price": "0.52", "best_bid": "0.52", "best_ask": "0.53", "side": "BUY"}
            ]
        }"#;

        let messages =
            parse_polymarket_message(json).expect("official array price_change should parse");
        assert_eq!(messages.len(), 2);

        match &messages[0] {
            PolymarketMessage::PriceChange {
                asset_id, best_ask, ..
            } => {
                assert_eq!(asset_id.as_deref(), Some("up_token"));
                assert_eq!(*best_ask, Some(0.48));
            }
            other => panic!("expected PriceChange, got {other:?}"),
        }
    }

    #[test]
    fn new_market_accepts_assets_ids_alias() {
        let json = r#"{
            "type": "new_market",
            "slug": "btc-updown-15m-1771695900",
            "assets_ids": ["token_up_123", "token_down_456"],
            "question": "Will BTC go up?"
        }"#;

        let msg = parse_polymarket_message(json).expect("new_market should parse");
        match msg.into_iter().next().unwrap() {
            PolymarketMessage::MarketInfo {
                slug, token_ids, ..
            } => {
                assert_eq!(slug.as_deref(), Some("btc-updown-15m-1771695900"));
                assert_eq!(token_ids.unwrap()[0], "token_up_123");
            }
            other => panic!("expected MarketInfo, got {other:?}"),
        }
    }
}
