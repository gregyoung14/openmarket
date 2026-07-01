use futures::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio::time::{sleep, Duration};
use tokio_tungstenite::connect_async;
use tracing::{error, info, warn};

use crate::config;
use crate::models::DbMessage;
use crate::normalize;
use crate::services::AppState;

#[derive(Debug, Clone)]
struct ActiveMarketInfo {
    slug: String,
    question: String,
    token_ids: Vec<String>,
    up_price: f64,
    down_price: f64,
}

fn parse_f64_value(v: &serde_json::Value) -> Option<f64> {
    if let Some(n) = v.as_f64() {
        return Some(n);
    }
    v.as_str().and_then(|s| s.parse::<f64>().ok())
}

/// How many 15-min windows ahead to pre-fetch and subscribe to.
/// 16 windows = 4 hours of coverage, ensuring even long WS outages have backfill.
const LOOKAHEAD_WINDOWS: i64 = 16;

/// Fetch a single market by its epoch start time.
async fn fetch_btc_market_by_epoch(epoch_start: i64) -> Option<ActiveMarketInfo> {
    let slug = format!("btc-updown-15m-{}", epoch_start);
    let url = format!("https://gamma-api.polymarket.com/events?slug={}", slug);

    let resp = reqwest::get(&url).await.ok()?;
    if !resp.status().is_success() {
        return None;
    }

    let events: serde_json::Value = resp.json().await.ok()?;
    let event = events.as_array()?.first()?;
    // Don't filter on closed — we want to subscribe even if it just closed
    // (so we catch final price_change events)

    let market = event.get("markets")?.as_array()?.first()?;

    let token_ids = market
        .get("clobTokenIds")
        .and_then(|v| v.as_str())
        .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
        .unwrap_or_default();
    if token_ids.len() < 2 {
        return None;
    }

    let prices: Vec<f64> = market
        .get("outcomePrices")
        .and_then(|v| v.as_str())
        .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
        .map(|x| {
            x.into_iter()
                .filter_map(|p| p.parse::<f64>().ok())
                .collect()
        })
        .unwrap_or_default();

    Some(ActiveMarketInfo {
        slug,
        question: market
            .get("question")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string(),
        token_ids,
        up_price: prices.first().copied().unwrap_or(0.5),
        down_price: prices.get(1).copied().unwrap_or(0.5),
    })
}

/// Fetch multiple upcoming markets (current + next LOOKAHEAD_WINDOWS).
/// Returns all that are available on Polymarket.
async fn fetch_upcoming_btc_markets() -> Vec<ActiveMarketInfo> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let interval: i64 = 15 * 60;
    let current_start = (now / interval) * interval;

    let mut markets = Vec::new();
    for i in 0..LOOKAHEAD_WINDOWS {
        let epoch = current_start + i * interval;
        match fetch_btc_market_by_epoch(epoch).await {
            Some(m) => markets.push(m),
            None => {
                // Future markets may not exist yet on Polymarket — stop looking further
                if i > 1 {
                    info!("Lookahead stopped at window {} (market not yet created)", i);
                    break;
                }
            }
        }
    }
    markets
}

fn convert_direct_book(item: &serde_json::Value) -> Option<serde_json::Value> {
    let asset_id = item.get("asset_id")?.as_str()?;
    let timestamp = item
        .get("timestamp")
        .cloned()
        .unwrap_or(serde_json::json!(0));

    let bids = item
        .get("bids")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| {
                    let price = x.get("price").and_then(parse_f64_value)?;
                    let size = x.get("size").and_then(parse_f64_value)?;
                    Some(serde_json::json!([price, size]))
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    let asks = item
        .get("asks")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| {
                    let price = x.get("price").and_then(parse_f64_value)?;
                    let size = x.get("size").and_then(parse_f64_value)?;
                    Some(serde_json::json!([price, size]))
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    Some(serde_json::json!({
        "type": "book",
        "asset_id": asset_id,
        "bids": bids,
        "asks": asks,
        "timestamp": timestamp
    }))
}

fn convert_direct_price_changes(item: &serde_json::Value) -> Vec<serde_json::Value> {
    let timestamp = item
        .get("timestamp")
        .cloned()
        .unwrap_or(serde_json::json!(0));

    item.get("price_changes")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| {
                    let asset_id = x.get("asset_id")?.as_str()?;
                    Some(serde_json::json!({
                        "type": "price_change",
                        "asset_id": asset_id,
                        "price": x.get("price").cloned(),
                        "size": x.get("size").cloned(),
                        "side": x.get("side").cloned(),
                        "best_bid": x.get("best_bid").cloned(),
                        "best_ask": x.get("best_ask").cloned(),
                        "timestamp": timestamp,
                    }))
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default()
}

pub fn spawn_db_writer(mut rx: mpsc::Receiver<DbMessage>) {
    tokio::spawn(async move {
        let mut b_buf = Vec::with_capacity(config::DB_BATCH_SIZE);
        let mut p_buf = Vec::with_capacity(config::DB_BATCH_SIZE);
        let mut m_buf = Vec::with_capacity(8);

        loop {
            match tokio::time::timeout(Duration::from_millis(config::DB_FLUSH_MS), rx.recv()).await
            {
                Ok(Some(msg)) => match msg {
                    DbMessage::Binance(v) => b_buf.push(v),
                    DbMessage::Polymarket(v) => p_buf.push(v),
                    DbMessage::MarketMeta(v) => m_buf.push(v),
                },
                Ok(None) => break,
                Err(_) => {}
            }

            if b_buf.len() >= config::DB_BATCH_SIZE
                || p_buf.len() >= config::DB_BATCH_SIZE
                || !m_buf.is_empty()
            {
                flush_buffers(&mut b_buf, &mut p_buf, &mut m_buf).await;
            }
        }

        flush_buffers(&mut b_buf, &mut p_buf, &mut m_buf).await;
    });
}

async fn flush_buffers(
    b_buf: &mut Vec<crate::models::BinanceTick>,
    p_buf: &mut Vec<crate::models::PolymarketTick>,
    m_buf: &mut Vec<crate::models::MarketMeta>,
) {
    if b_buf.is_empty() && p_buf.is_empty() && m_buf.is_empty() {
        return;
    }

    let b = std::mem::take(b_buf);
    let p = std::mem::take(p_buf);
    let m = std::mem::take(m_buf);

    let res = tokio::task::spawn_blocking(move || {
        let mut conn = crate::db::get_db_conn()?;
        let _ = crate::db::insert_binance_ticks(&mut conn, &b)?;
        let _ = crate::db::insert_polymarket_ticks(&mut conn, &p)?;
        let _ = crate::db::upsert_market_meta(&mut conn, &m)?;
        Ok::<(), anyhow::Error>(())
    })
    .await;

    if let Err(e) = res {
        error!("DB flush task join error: {}", e);
    }
}

pub fn spawn_binance_ingestor(state: AppState) {
    tokio::spawn(async move {
        let mut attempt = 0u32;
        loop {
            match run_binance_connection(&state).await {
                Ok(()) => attempt = 0,
                Err(e) => {
                    attempt += 1;
                    error!("Binance ingest error: {}", e);
                }
            }

            let backoff = Duration::from_millis(500 * (2u64.pow(attempt.min(7))));
            sleep(backoff).await;
        }
    });
}

async fn run_binance_connection(state: &AppState) -> anyhow::Result<()> {
    use tokio_tungstenite::tungstenite::protocol::Message;

    let (mut ws, _) = connect_async(config::BINANCE_WS_URL).await?;
    info!(
        "Connected to recorder source Binance WS {}",
        config::BINANCE_WS_URL
    );

    let mut ping_interval = tokio::time::interval(Duration::from_secs(config::PING_INTERVAL_SECS));
    let mut last_activity = tokio::time::Instant::now();

    loop {
        tokio::select! {
            _ = ping_interval.tick() => {
                ws.send(Message::Ping(vec![1,2,3].into())).await?;
            }
            msg = ws.next() => {
                let Some(msg) = msg else { break; };
                let msg = msg?;
                last_activity = tokio::time::Instant::now();

                match msg {
                    Message::Text(text) => {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                            if let Some(t) = normalize::normalize_binance_message(&v) {
                                let _ = state.db_tx.send(DbMessage::Binance(t)).await;
                                state.inc_binance();
                            }
                        }
                    }
                    Message::Ping(payload) => {
                        ws.send(Message::Pong(payload)).await?;
                    }
                    Message::Close(_) => break,
                    _ => {}
                }
            }
        }

        if last_activity.elapsed() > Duration::from_secs(config::PONG_TIMEOUT_SECS) {
            warn!("Binance source idle timeout");
            break;
        }
    }

    Ok(())
}

pub fn spawn_polymarket_ingestor(state: AppState) {
    tokio::spawn(async move {
        let mut attempt = 0u32;
        loop {
            match run_polymarket_connection(&state).await {
                Ok(()) => attempt = 0,
                Err(e) => {
                    attempt += 1;
                    error!("Polymarket ingest error: {}", e);
                }
            }

            let backoff = Duration::from_millis(500 * (2u64.pow(attempt.min(7))));
            sleep(backoff).await;
        }
    });
}

async fn run_polymarket_connection(state: &AppState) -> anyhow::Result<()> {
    use std::collections::HashSet;
    use tokio_tungstenite::tungstenite::protocol::Message;

    let (mut ws, _) = connect_async(config::POLYMARKET_WS_URL).await?;
    info!(
        "Connected to recorder source Polymarket WS {}",
        config::POLYMARKET_WS_URL
    );

    let mut ping_interval = tokio::time::interval(Duration::from_secs(config::PING_INTERVAL_SECS));
    // Check for new markets every ~14 minutes (just before the next 15-min boundary)
    let mut market_refresh_interval = tokio::time::interval(Duration::from_secs(14 * 60));
    let mut last_activity = tokio::time::Instant::now();

    // Track all token IDs we've subscribed to so we don't re-subscribe
    let mut subscribed_tokens: HashSet<String> = HashSet::new();

    // ── On connect: subscribe to ALL upcoming markets (current + next 4 hours) ──
    let upcoming = fetch_upcoming_btc_markets().await;
    info!(
        "Fetched {} upcoming markets for bulk subscription",
        upcoming.len()
    );

    let mut all_token_ids: Vec<String> = Vec::new();
    for market in &upcoming {
        // Register in asset_map + DB
        {
            let mut map = state.token_mapping.write().await;
            // Always point "current" to the first (active) market
            if map.market_slug.is_none() {
                map.market_slug = Some(market.slug.clone());
                map.up_token_id = Some(market.token_ids[0].clone());
                map.down_token_id = Some(market.token_ids[1].clone());
            }
            map.asset_map.insert(
                market.token_ids[0].clone(),
                crate::services::AssetInfo {
                    side: "UP".to_string(),
                    market_slug: market.slug.clone(),
                },
            );
            map.asset_map.insert(
                market.token_ids[1].clone(),
                crate::services::AssetInfo {
                    side: "DOWN".to_string(),
                    market_slug: market.slug.clone(),
                },
            );
        }

        // Upsert market_meta
        let meta = crate::models::MarketMeta {
            market_slug: market.slug.clone(),
            question: market.question.clone(),
            up_token_id: market.token_ids[0].clone(),
            down_token_id: market.token_ids[1].clone(),
            up_price: market.up_price,
            down_price: market.down_price,
            first_seen_ms: chrono_like_now_ms(),
            last_seen_ms: chrono_like_now_ms(),
        };
        {
            let mut lm = state.last_market.write().await;
            *lm = Some(meta.clone());
        }
        let _ = state.db_tx.send(DbMessage::MarketMeta(meta)).await;

        // Collect token IDs for subscription
        for tid in &market.token_ids {
            if subscribed_tokens.insert(tid.clone()) {
                all_token_ids.push(tid.clone());
            }
        }
    }

    // Subscribe to ALL tokens in one message
    if !all_token_ids.is_empty() {
        let sub = serde_json::json!({"assets_ids": all_token_ids, "type": "market"});
        ws.send(Message::Text(sub.to_string().into())).await?;
        info!(
            "Subscribed to {} token IDs across {} markets ({}h coverage)",
            all_token_ids.len(),
            upcoming.len(),
            upcoming.len() as f64 * 0.25
        );
    }

    loop {
        tokio::select! {
            _ = ping_interval.tick() => {
                ws.send(Message::Ping(vec![4,5,6].into())).await?;
            }
            _ = market_refresh_interval.tick() => {
                // Every ~14 min: fetch upcoming markets and subscribe to any new ones
                let upcoming = fetch_upcoming_btc_markets().await;
                let mut new_tokens: Vec<String> = Vec::new();

                for market in &upcoming {
                    // Update "current" pointer to the first/active market
                    {
                        let mut map = state.token_mapping.write().await;
                        map.market_slug = Some(market.slug.clone());
                        map.up_token_id = Some(market.token_ids[0].clone());
                        map.down_token_id = Some(market.token_ids[1].clone());
                        map.asset_map.insert(market.token_ids[0].clone(), crate::services::AssetInfo {
                            side: "UP".to_string(),
                            market_slug: market.slug.clone(),
                        });
                        map.asset_map.insert(market.token_ids[1].clone(), crate::services::AssetInfo {
                            side: "DOWN".to_string(),
                            market_slug: market.slug.clone(),
                        });
                    }

                    let meta = crate::models::MarketMeta {
                        market_slug: market.slug.clone(),
                        question: market.question.clone(),
                        up_token_id: market.token_ids[0].clone(),
                        down_token_id: market.token_ids[1].clone(),
                        up_price: market.up_price,
                        down_price: market.down_price,
                        first_seen_ms: chrono_like_now_ms(),
                        last_seen_ms: chrono_like_now_ms(),
                    };
                    {
                        let mut lm = state.last_market.write().await;
                        *lm = Some(meta.clone());
                    }
                    let _ = state.db_tx.send(DbMessage::MarketMeta(meta)).await;

                    for tid in &market.token_ids {
                        if subscribed_tokens.insert(tid.clone()) {
                            new_tokens.push(tid.clone());
                        }
                    }
                }

                if !new_tokens.is_empty() {
                    let sub = serde_json::json!({"assets_ids": new_tokens, "type": "market"});
                    ws.send(Message::Text(sub.to_string().into())).await?;
                    info!(
                        "Added {} new token IDs ({} total subscribed across {} markets)",
                        new_tokens.len(),
                        subscribed_tokens.len(),
                        upcoming.len()
                    );
                }
            }
            msg = ws.next() => {
                let Some(msg) = msg else { break; };
                let msg = msg?;
                last_activity = tokio::time::Instant::now();

                match msg {
                    Message::Text(text) => {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                            let map = state.token_mapping.read().await.clone();

                            if let Some(items) = v.as_array() {
                                for item in items {
                                    if item.get("event_type").and_then(|x| x.as_str()) == Some("book") {
                                        if let Some(norm) = convert_direct_book(item) {
                                            let rows = normalize::normalize_polymarket_message(&norm, &map);
                                            for r in rows {
                                                let _ = state.db_tx.send(DbMessage::Polymarket(r)).await;
                                                state.inc_polymarket();
                                            }
                                        }
                                    }
                                }
                            } else if v.get("event_type").and_then(|x| x.as_str()) == Some("price_change") {
                                for norm in convert_direct_price_changes(&v) {
                                    let rows = normalize::normalize_polymarket_message(&norm, &map);
                                    for r in rows {
                                        let _ = state.db_tx.send(DbMessage::Polymarket(r)).await;
                                        state.inc_polymarket();
                                    }
                                }
                            }
                        }
                    }
                    Message::Ping(payload) => {
                        ws.send(Message::Pong(payload)).await?;
                    }
                    Message::Close(_) => break,
                    _ => {}
                }
            }
        }

        if last_activity.elapsed() > Duration::from_secs(config::PONG_TIMEOUT_SECS) {
            warn!("Polymarket source idle timeout");
            break;
        }
    }

    Ok(())
}

fn chrono_like_now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}
