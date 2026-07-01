use crate::config;
use crate::services::AppState;
use anyhow::Result;
use futures::sink::SinkExt;
use futures::stream::StreamExt;
use serde_json::{json, Value};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::broadcast;
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tracing::{error, info, warn};

#[derive(Debug, Clone)]
struct ActiveMarketInfo {
    slug: String,
    question: String,
    token_ids: Vec<String>,
    up_price: f64,
    down_price: f64,
}

fn parse_f64_value(v: &Value) -> Option<f64> {
    if let Some(n) = v.as_f64() {
        return Some(n);
    }
    v.as_str().and_then(|s| s.parse::<f64>().ok())
}

fn parse_string_vec(value: Option<&Value>) -> Vec<String> {
    let Some(raw) = value else {
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

async fn fetch_active_btc_market() -> Option<ActiveMarketInfo> {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs() as i64;
    let interval = 15 * 60;
    let current_start = (now / interval) * interval;
    let slug = format!("btc-updown-15m-{}", current_start);
    let url = format!("https://gamma-api.polymarket.com/events?slug={}", slug);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(config::HTTP_TIMEOUT_SECS))
        .build()
        .ok()?;
    let resp = match client.get(&url).send().await {
        Ok(r) => r,
        Err(e) => {
            warn!("Failed to fetch current market {}: {}", slug, e);
            return None;
        }
    };

    if !resp.status().is_success() {
        return None;
    }

    let events: Value = match resp.json().await {
        Ok(v) => v,
        Err(e) => {
            warn!("Invalid market response for {}: {}", slug, e);
            return None;
        }
    };

    let event = events.as_array().and_then(|arr| arr.first())?;

    // Do not hard-require `active=true` because Gamma can lag around interval boundaries.
    // If current-slug market exists and is not closed, we can subscribe immediately.
    let is_closed = event
        .get("closed")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if is_closed {
        return None;
    }

    let market = event
        .get("markets")
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first())?;

    let token_ids = parse_string_vec(market.get("clobTokenIds"));

    if token_ids.len() < 2 {
        return None;
    }

    let outcome_prices: Vec<f64> = parse_string_vec(market.get("outcomePrices"))
        .into_iter()
        .filter_map(|p| p.parse::<f64>().ok())
        .collect();

    let outcomes: Vec<String> = parse_string_vec(market.get("outcomes"));

    let mut up_idx = 0usize;
    let mut down_idx = 1usize.min(token_ids.len().saturating_sub(1));

    if outcomes.len() >= 2 && token_ids.len() >= 2 {
        let normalized: Vec<String> = outcomes
            .iter()
            .map(|o| o.trim().to_ascii_lowercase())
            .collect();

        let yes_idx = normalized
            .iter()
            .position(|o| o == "yes" || o == "up" || o.contains("higher") || o.contains("above"));
        let no_idx = normalized
            .iter()
            .position(|o| o == "no" || o == "down" || o.contains("lower") || o.contains("below"));

        if let (Some(u), Some(d)) = (yes_idx, no_idx) {
            if u < token_ids.len() && d < token_ids.len() && u != d {
                up_idx = u;
                down_idx = d;
            }
        }
    }

    let up_price = outcome_prices.get(up_idx).copied().unwrap_or(0.5);
    let down_price = outcome_prices.get(down_idx).copied().unwrap_or(0.5);
    let ordered_token_ids = vec![token_ids[up_idx].clone(), token_ids[down_idx].clone()];
    let question = market
        .get("question")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();

    Some(ActiveMarketInfo {
        slug,
        question,
        token_ids: ordered_token_ids,
        up_price,
        down_price,
    })
}

/// Connect to Polymarket WebSocket and stream market data.
/// MISSION CRITICAL: near-zero downtime, instant reconnect, ms-precision.
pub async fn polymarket_reader_task(tx_broadcast: broadcast::Sender<String>, state: AppState) {
    let mut attempt = 0u32;
    let mut last_market_id: Option<String> = None;

    loop {
        info!(
            attempt = attempt + 1,
            "Connecting to Polymarket upstream WS"
        );

        match run_websocket_connection(&tx_broadcast, &last_market_id, &state).await {
            Ok((market_id, _)) => {
                last_market_id = Some(market_id);
                warn!("Upstream WS ended gracefully — reconnecting IMMEDIATELY");
                attempt = 0;
            }
            Err(e) => {
                error!(error = %e, attempt, "Upstream WS error — reconnecting");
                attempt += 1;
            }
        }

        state.set_upstream_connected(false);

        // Ultra-aggressive reconnect: 50ms base, caps at 2s, tiny jitter
        let base_delay = Duration::from_millis(config::RECONNECT_BASE_DELAY_MS);
        let exp = attempt.min(6); // caps at 2^6 * 50ms = 3.2s, but MAX caps at 2s
        let backoff = base_delay.saturating_mul(2u32.pow(exp));
        let max_delay = Duration::from_secs(config::MAX_RECONNECT_DELAY_SECS);
        let delay = backoff.min(max_delay);
        let jitter = Duration::from_millis(rand::random::<u64>() % 50);
        let total_delay = delay + jitter;

        warn!(delay_ms = total_delay.as_millis() as u64, "Reconnecting in");
        sleep(total_delay).await;
    }
}

/// Run a single WebSocket connection — ultra-low-latency, instant broadcast, stale-data watchdog.
async fn run_websocket_connection(
    tx_broadcast: &broadcast::Sender<String>,
    market_id: &Option<String>,
    state: &AppState,
) -> Result<(String, ()), Box<dyn std::error::Error + Send + Sync>> {
    use tokio_tungstenite::tungstenite::protocol::Message as TungsteniteMessage;

    let (mut ws_stream, _) = tokio::time::timeout(
        Duration::from_secs(config::WS_CONNECT_TIMEOUT_SECS),
        connect_async(config::POLYMARKET_WS_URL),
    )
    .await
    .map_err(|_| -> Box<dyn std::error::Error + Send + Sync> {
        "WebSocket connect timeout".into()
    })??;
    info!("✅ Connected to Polymarket upstream WS");
    state.set_upstream_connected(true);

    let mut last_activity = std::time::Instant::now();
    let mut last_data_received = std::time::Instant::now(); // tracks actual data (book/price_change)
    let mut ping_interval = tokio::time::interval(Duration::from_secs(config::PING_INTERVAL_SECS));
    let mut stale_check_interval = tokio::time::interval(Duration::from_secs(1));
    let pong_timeout = Duration::from_secs(config::PONG_TIMEOUT_SECS);
    let stale_data_timeout = Duration::from_secs(config::STALE_DATA_TIMEOUT_SECS);
    let mut current_market = market_id.clone().unwrap_or_default();
    let mut msg_count: u64 = 0;

    // Compute when the next 15-minute boundary hits so we only call Gamma API then
    let next_boundary_sleep = {
        let now_epoch = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let interval = config::MARKET_INTERVAL_SECS;
        let next_boundary = ((now_epoch / interval) + 1) * interval;
        // Wake up 1s before boundary to be ready instantly
        let secs_until = (next_boundary - now_epoch - 1).max(0) as u64;
        tokio::time::sleep(Duration::from_secs(secs_until))
    };
    tokio::pin!(next_boundary_sleep);

    // Immediately subscribe to current market
    if let Some(active_market) = fetch_active_btc_market().await {
        current_market = active_market.slug.clone();

        let subscription = json!({
            "assets_ids": active_market.token_ids,
            "type": "market"
        })
        .to_string();

        ws_stream
            .send(TungsteniteMessage::Text(subscription.into()))
            .await?;

        let market_msg = json!({
            "type": "market_info",
            "slug": active_market.slug,
            "question": active_market.question,
            "up_price": active_market.up_price,
            "down_price": active_market.down_price,
            "token_ids": active_market.token_ids,
            "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
        })
        .to_string();
        state.set_market(current_market.clone());
        state.set_token_sides(&active_market.token_ids);
        state.set_market_message(market_msg.clone());
        let _ = tx_broadcast.send(market_msg);
        last_data_received = std::time::Instant::now();

        info!(slug = %current_market, tokens = ?active_market.token_ids, "Subscribed to market");
    } else {
        warn!("No active BTC 15m market found — will retry at next boundary");
    }

    loop {
        tokio::select! {
            // Bias towards reading upstream data — this is the hot path
            biased;

            // READ UPSTREAM DATA FIRST (highest priority)
            Some(msg) = ws_stream.next() => {
                match msg {
                    Ok(message) => {
                        last_activity = std::time::Instant::now();

                        match message {
                            TungsteniteMessage::Ping(payload) => {
                                let _ = ws_stream.send(TungsteniteMessage::Pong(payload)).await;
                            }
                            TungsteniteMessage::Pong(_) => {
                                // good, connection alive
                            }
                            TungsteniteMessage::Text(text) => {
                                let now_ms = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64;
                                state.record_upstream_message(now_ms);
                                last_data_received = std::time::Instant::now();
                                // Process and broadcast immediately
                                if let Ok(data) = serde_json::from_str::<Value>(&text) {
                                    if let Some(items) = data.as_array() {
                                        for item in items {
                                            msg_count += 1;
                                            process_market_message(item, tx_broadcast, &mut current_market, state);
                                        }
                                    } else {
                                        msg_count += 1;
                                        process_market_message(&data, tx_broadcast, &mut current_market, state);
                                    }
                                }
                            }
                            TungsteniteMessage::Close(frame) => {
                                warn!(frame = ?frame, "Upstream sent close — will reconnect");
                                return Ok((current_market.clone(), ()));
                            }
                            _ => {}
                        }
                    }
                    Err(e) => {
                        error!(error = %e, "Upstream read error");
                        return Err(e.into());
                    }
                }
            }

            // Keep-alive ping
            _ = ping_interval.tick() => {
                if let Err(e) = ws_stream.send(TungsteniteMessage::Ping(vec![1, 2, 3, 4].into())).await {
                    error!(error = %e, "Failed to send ping");
                    return Err(Box::new(e) as Box<dyn std::error::Error + Send + Sync>);
                }
            }

            // Boundary-aligned market refresh — fires once right at each 15-min mark
            _ = &mut next_boundary_sleep => {
                info!("⏰ 15-minute boundary reached — fetching new market from Gamma API");

                // Wrap entire fetch+retry in a timeout so we never block the select loop
                let fetch_result = tokio::time::timeout(
                    Duration::from_secs(config::BOUNDARY_FETCH_TIMEOUT_SECS),
                    async {
                        for retry in 0..10 {
                            if let Some(active_market) = fetch_active_btc_market().await {
                                return Some((active_market, retry));
                            }
                            warn!(retry, "Gamma API not ready yet — retrying in 500ms");
                            sleep(Duration::from_millis(500)).await;
                        }
                        None
                    },
                )
                .await;

                match fetch_result {
                    Ok(Some((active_market, retry))) => {
                        if active_market.slug != current_market {
                            current_market = active_market.slug.clone();

                            let subscription = json!({
                                "assets_ids": active_market.token_ids,
                                "type": "market"
                            })
                            .to_string();

                            ws_stream.send(TungsteniteMessage::Text(subscription.into())).await?;

                            let market_msg = json!({
                                "type": "market_info",
                                "slug": active_market.slug,
                                "question": active_market.question,
                                "up_price": active_market.up_price,
                                "down_price": active_market.down_price,
                                "token_ids": active_market.token_ids,
                                "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
                            })
                            .to_string();

                            state.set_market(current_market.clone());
                            state.set_token_sides(&active_market.token_ids);
                            state.set_market_message(market_msg.clone());
                            let _ = tx_broadcast.send(market_msg);
                            last_data_received = std::time::Instant::now();

                            info!(slug = %current_market, retry, "🔄 Switched to new market — resubscribed");
                        }
                    }
                    Ok(None) => {
                        error!("Failed to fetch new market after retries at boundary");
                    }
                    Err(_) => {
                        error!("Boundary market fetch TIMED OUT after {}s — select loop unblocked",
                            config::BOUNDARY_FETCH_TIMEOUT_SECS);
                    }
                }

                // Schedule next boundary sleep
                let now_epoch = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
                let interval = config::MARKET_INTERVAL_SECS;
                let next_boundary = ((now_epoch / interval) + 1) * interval;
                let secs_until = (next_boundary - now_epoch - 1).max(0) as u64;
                next_boundary_sleep.set(tokio::time::sleep(Duration::from_secs(secs_until)));
            }

            // Stale-data watchdog — if no real data for N seconds, force reconnect
            _ = stale_check_interval.tick() => {
                let data_age = last_data_received.elapsed();
                if data_age > stale_data_timeout {
                    error!(
                        stale_secs = data_age.as_secs(),
                        total_msgs = msg_count,
                        "No upstream data for {}s — STALE CONNECTION, forcing reconnect",
                        data_age.as_secs()
                    );
                    return Err("Stale data timeout — no upstream messages".into());
                }

                // Log throughput every 10s for observability
                if msg_count > 0 && msg_count.is_multiple_of(100) {
                    info!(total_msgs = msg_count, "Upstream throughput checkpoint");
                }
            }

            else => break,
        }

        // Safety net: absolute idle timeout
        if last_activity.elapsed() > pong_timeout {
            error!(
                idle_secs = last_activity.elapsed().as_secs(),
                "Idle timeout exceeded — reconnecting"
            );
            return Err("Connection idle timeout".into());
        }
    }

    Ok((current_market, ()))
}

/// Process incoming market messages from Polymarket
fn process_market_message(
    data: &Value,
    tx_broadcast: &broadcast::Sender<String>,
    market_id: &mut String,
    state: &AppState,
) {
    let event_type = data.get("event_type").and_then(|v| v.as_str());
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;

    match event_type {
        Some("book") => {
            state.record_market_data(now_ms);
            // Full order book snapshot
            if let Some(asset_id) = data.get("asset_id").and_then(|v| v.as_str()) {
                *market_id = asset_id.to_string();

                // Resolve asset_id to UP/DOWN side
                let resolved_side = state.get_side_for_token(asset_id);

                let bids: Vec<[f64; 2]> = data
                    .get("bids")
                    .and_then(|v| v.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|item| {
                                let price = item.get("price").and_then(parse_f64_value)?;
                                let size = item.get("size").and_then(parse_f64_value)?;
                                Some([price, size])
                            })
                            .collect()
                    })
                    .unwrap_or_default();

                let asks: Vec<[f64; 2]> = data
                    .get("asks")
                    .and_then(|v| v.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|item| {
                                let price = item.get("price").and_then(parse_f64_value)?;
                                let size = item.get("size").and_then(parse_f64_value)?;
                                Some([price, size])
                            })
                            .collect()
                    })
                    .unwrap_or_default();

                let timestamp = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64;

                let mut msg_value = json!({
                    "type": "book",
                    "asset_id": asset_id,
                    "bids": bids,
                    "asks": asks,
                    "timestamp": timestamp
                });
                // Include resolved side so downstream doesn't need its own token map
                if let Some(ref side) = resolved_side {
                    msg_value["side"] = json!(side);
                }

                let _ = tx_broadcast.send(msg_value.to_string());
                info!(
                    "📊 Book snapshot for {} (side={:?})",
                    asset_id, resolved_side
                );
            }
        }
        Some("price_change") => {
            state.record_market_data(now_ms);
            // Price change updates
            if let Some(changes) = data.get("price_changes").and_then(|v| v.as_array()) {
                for change in changes {
                    if let (Some(asset_id), Some(price), Some(size), Some(order_side)) = (
                        change.get("asset_id").and_then(|v| v.as_str()),
                        change.get("price").and_then(parse_f64_value),
                        change.get("size").and_then(parse_f64_value),
                        change.get("side").and_then(|v| v.as_str()),
                    ) {
                        let best_bid = change.get("best_bid").and_then(parse_f64_value);
                        let best_ask = change.get("best_ask").and_then(parse_f64_value);
                        let timestamp = SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .unwrap()
                            .as_millis() as i64;

                        // Resolve asset_id to UP/DOWN side
                        let resolved_side = state.get_side_for_token(asset_id);

                        let mut msg_value = json!({
                            "type": "price_change",
                            "asset_id": asset_id,
                            "price": price,
                            "size": size,
                            "side": order_side.to_uppercase(),
                            "best_bid": best_bid,
                            "best_ask": best_ask,
                            "timestamp": timestamp
                        });
                        // Include resolved UP/DOWN side for downstream
                        if let Some(ref side) = resolved_side {
                            msg_value["market_side"] = json!(side);
                        }

                        let _ = tx_broadcast.send(msg_value.to_string());
                        info!(
                            "💹 Price change: {} {} @ {} (market_side={:?})",
                            asset_id, order_side, price, resolved_side
                        );
                    }
                }
            }
        }
        Some("last_trade_price") => {
            state.record_market_data(now_ms);
            // Trade updates
            if let (Some(asset_id), Some(price), Some(size), Some(side)) = (
                data.get("asset_id").and_then(|v| v.as_str()),
                data.get("price").and_then(parse_f64_value),
                data.get("size").and_then(parse_f64_value),
                data.get("side").and_then(|v| v.as_str()),
            ) {
                let timestamp = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64;

                // Resolve asset_id to UP/DOWN side
                let resolved_side = state.get_side_for_token(asset_id);

                let mut msg_value = json!({
                    "type": "trade",
                    "asset_id": asset_id,
                    "price": price,
                    "size": size,
                    "side": side.to_uppercase(),
                    "timestamp": timestamp
                });
                if let Some(ref side) = resolved_side {
                    msg_value["market_side"] = json!(side);
                }

                let _ = tx_broadcast.send(msg_value.to_string());
                info!(
                    "🔔 Trade: {} {} @ {} (market_side={:?})",
                    asset_id, side, price, resolved_side
                );
            }
        }
        _ => {
            if let Some(et) = event_type {
                warn!("Unhandled event type: {}", et);
            }
        }
    }
}
