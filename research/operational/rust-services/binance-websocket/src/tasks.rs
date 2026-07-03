use crate::{
    config, db,
    models::{Candle, Trade},
    services::AppState,
};
use futures::sink::SinkExt;
use futures::stream::StreamExt;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{broadcast, mpsc};
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tracing::{error, info};

/// Spawn the database writer task that batches trades
pub fn spawn_db_writer(mut rx_db_write: mpsc::Receiver<Trade>) {
    tokio::spawn(async move {
        let mut buffer: Vec<Trade> = Vec::with_capacity(config::TRADE_BUFFER_SIZE);
        while let Some(trade) = rx_db_write.recv().await {
            buffer.push(trade);
            if buffer.len() >= config::TRADE_BUFFER_SIZE {
                let batch: Vec<Trade> = std::mem::take(&mut buffer);
                tokio::task::spawn_blocking(move || {
                    if let Ok(mut conn) = db::get_db_conn() {
                        if let Ok(tx) = conn.transaction() {
                            if let Ok(mut stmt) = tx.prepare(
                                "INSERT OR IGNORE INTO binance_trades
                                (trade_id, trade_time, price, quantity, quote_volume, is_buyer_maker, received_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)",
                            ) {
                                for t in &batch {
                                    let _ = stmt.execute((
                                        t.trade_id,
                                        t.trade_time,
                                        t.price,
                                        t.quantity,
                                        t.quote_volume,
                                        t.is_buyer_maker,
                                        t.received_at,
                                    ));
                                }
                            }
                            let _ = tx.commit();
                            info!("✅ Wrote {} trades to database", batch.len());
                        }
                    } else {
                        error!("Failed to get DB conn for batch write");
                    }
                })
                .await
                .ok();
            }
        }
    });
}

/// Connect to Binance WebSocket and stream trades with robust reconnection
pub async fn binance_reader_task(
    state: AppState,
    tx_db: mpsc::Sender<Trade>,
) {
    let mut trade_count = 0;
    let mut attempt = 0u32;

    loop {
        info!("Connecting to Binance (attempt {})...", attempt + 1);

        match run_websocket_connection(&state, &tx_db, &mut trade_count).await {
            Ok(()) => {
                info!("WebSocket connection ended gracefully");
                attempt = 0; // Reset on clean close
            }
            Err(e) => {
                error!("WebSocket error: {}", e);
                attempt += 1;
            }
        }

        // Exponential backoff with jitter
        let base_delay = Duration::from_millis(config::RECONNECT_BASE_DELAY_MS);
        let exp = attempt.min(10); // Cap at 2^10
        let backoff = base_delay.saturating_mul(2u32.pow(exp));
        let max_delay = Duration::from_secs(config::MAX_RECONNECT_DELAY_SECS);
        let delay = backoff.min(max_delay);

        // Add jitter (0-1000ms)
        let jitter = Duration::from_millis(rand::random::<u64>() % 1000);
        let total_delay = delay + jitter;

        info!("Reconnecting in {:?}", total_delay);
        sleep(total_delay).await;
    }
}

/// Run a single WebSocket connection with keep-alive and proper ping/pong handling
async fn run_websocket_connection(
    state: &AppState,
    tx_db: &mpsc::Sender<Trade>,
    trade_count: &mut usize,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use tokio::time::Instant;
    use tokio_tungstenite::tungstenite::protocol::Message as TungsteniteMessage;

    let (mut ws_stream, _) = connect_async(config::BINANCE_WS_URL).await?;
    info!("✅ Connected to Binance aggTrade stream");
    state.set_binance_ws_connected(true);

    let mut last_activity = Instant::now();
    let mut ping_interval = tokio::time::interval(Duration::from_secs(config::PING_INTERVAL_SECS));
    let pong_timeout = Duration::from_secs(config::PONG_TIMEOUT_SECS);

    loop {
        tokio::select! {
            // Send periodic keep-alive ping
            _ = ping_interval.tick() => {
                if let Err(e) = ws_stream.send(TungsteniteMessage::Ping(vec![1, 2, 3, 4].into())).await {
                    error!("Failed to send keep-alive ping: {}", e);
                    return Err(e.into());
                }
                info!("Sent keep-alive ping");
            }

            // Read messages from Binance
            Some(msg) = ws_stream.next() => {
                match msg {
                    Ok(message) => {
                        last_activity = Instant::now();

                        match message {
                            TungsteniteMessage::Ping(payload) => {
                                // Binance sent ping - MUST respond with pong
                                if let Err(e) = ws_stream.send(TungsteniteMessage::Pong(payload)).await {
                                    error!("Failed to send pong: {}", e);
                                    return Err(e.into());
                                }
                                info!("Replied to Binance ping");
                            }
                            TungsteniteMessage::Pong(_) => {
                                info!("Received pong response");
                            }
                            TungsteniteMessage::Text(text) => {
                                // Process trade data
                                if let Ok(data) = serde_json::from_str::<Value>(&text) {
                                    let received_at = SystemTime::now()
                                        .duration_since(UNIX_EPOCH)
                                        .unwrap()
                                        .as_millis() as i64;

                                    let trade_id = data["a"].as_i64().unwrap_or(0);
                                    let price = data["p"]
                                        .as_str()
                                        .unwrap_or("0")
                                        .parse::<f64>()
                                        .unwrap_or(0.0);
                                    let quantity = data["q"]
                                        .as_str()
                                        .unwrap_or("0")
                                        .parse::<f64>()
                                        .unwrap_or(0.0);
                                    let trade_time = data["T"].as_i64().unwrap_or(0);
                                    let is_buyer_maker =
                                        if data["m"].as_bool().unwrap_or(false) { 1 } else { 0 };
                                    let quote_volume = price * quantity;

                                    let trade = Trade {
                                        trade_id,
                                        trade_time,
                                        price,
                                        quantity,
                                        quote_volume,
                                        is_buyer_maker,
                                        received_at,
                                    };

                                    state.record_upstream_trade_received(received_at);

                                    // Send to DB Writer
                                    let _ = tx_db.send(trade.clone()).await;
                                    *trade_count += 1;
                                    if (*trade_count).is_multiple_of(100) {
                                        info!("📊 Processed {} trades", trade_count);
                                    }

                                    // Broadcast to WebSocket clients
                                    let msg_out = json!({
                                        "type": "trade",
                                        "timestamp": received_at,
                                        "price": price,
                                        "volume": quantity,
                                        "time": trade_time
                                    })
                                    .to_string();
                                    let _ = state.tx.send(msg_out);
                                    state.record_trade_broadcast(received_at);
                                }
                            }
                            TungsteniteMessage::Close(frame) => {
                                info!("Received close frame: {:?}", frame);
                                state.set_binance_ws_connected(false);
                                return Ok(());
                            }
                            _ => {}
                        }
                    }
                    Err(e) => {
                        error!("Read error: {}", e);
                        state.set_binance_ws_connected(false);
                        return Err(e.into());
                    }
                }
            }

            else => break,
        }

        // Safety net: if no activity for too long, assume connection is dead
        if last_activity.elapsed() > pong_timeout {
            error!(
                "No activity for {:?} - assuming dead connection",
                pong_timeout
            );
            state.set_binance_ws_connected(false);
            return Err("Connection idle timeout".into());
        }
    }

    state.set_binance_ws_connected(false);
    Ok(())
}

/// Aggregate trades into candles periodically
pub async fn aggregator_task(tx_broadcast: broadcast::Sender<String>) {
    let mut last_processed: HashMap<String, i64> = HashMap::new();

    info!("🕐 Candle aggregator started");

    loop {
        sleep(Duration::from_secs(1)).await;
        let current_time = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;

        let last_processed_snapshot = last_processed.clone();
        let intervals = config::INTERVALS.to_vec();

        let processed_candles = tokio::task::spawn_blocking(move || {
            let conn = match db::get_db_conn() {
                Ok(c) => c,
                Err(_) => return vec![],
            };

            let mut updates = Vec::new();

            for (interval_name, interval_ms) in &intervals {
                let last_time = *last_processed_snapshot
                    .get(*interval_name)
                    .unwrap_or(&(current_time - interval_ms * 100));

                let stmt = conn.prepare(
                    "SELECT trade_time, price, quantity, quote_volume
                    FROM binance_trades
                    WHERE trade_time > ? AND trade_time <= ?
                    ORDER BY trade_time ASC",
                );

                if let Ok(mut stmt) = stmt {
                    let trade_iter = stmt.query_map([last_time, current_time], |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, f64>(1)?,
                            row.get::<_, f64>(2)?,
                            row.get::<_, f64>(3)?,
                        ))
                    });

                    if let Ok(rows) = trade_iter {
                        // (open, high, low, close, volume, quote_volume, count)
                        type CandleRow = (f64, f64, f64, f64, f64, f64, i64);
                        let mut candles: HashMap<i64, CandleRow> = HashMap::new();

                        for trade in rows.flatten() {
                            let (t_time, price, qty, qvol) = trade;
                            let candle_start = (t_time / interval_ms) * interval_ms;
                            let entry = candles
                                .entry(candle_start)
                                .or_insert((price, price, price, price, 0.0, 0.0, 0));
                            entry.1 = entry.1.max(price); // high
                            entry.2 = entry.2.min(price); // low
                            entry.3 = price; // close
                            entry.4 += qty; // volume
                            entry.5 += qvol;
                            entry.6 += 1;
                        }

                        // Collect and sort candles by time to ensure indicators are calculated in order
                        let mut sorted_keys: Vec<_> = candles.keys().collect();
                        sorted_keys.sort();

                        for candle_start in sorted_keys {
                            let (open, high, low, close, vol, qvol, count) = candles[candle_start];
                            let candle_end = candle_start + interval_ms - 1;

                            if current_time > candle_end {
                                let _ = conn.execute(
                                    &format!(
                                        "INSERT OR REPLACE INTO binance_candles_{}
                                        (candle_start, candle_end, open_price, high_price, low_price, close_price, volume, quote_volume, trade_count, created_at)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        interval_name
                                    ),
                                    (
                                        candle_start,
                                        candle_end,
                                        open,
                                        high,
                                        low,
                                        close,
                                        vol,
                                        qvol,
                                        count,
                                        current_time,
                                    ),
                                );

                                updates.push(Candle {
                                    interval: interval_name.to_string(),
                                    time: *candle_start,
                                    open,
                                    high,
                                    low,
                                    close,
                                    volume: vol,
                                });
                            }
                        }
                    }
                }
            }
            updates
        })
        .await
        .unwrap();

        // Broadcast updates
        if !processed_candles.is_empty() {
            info!("📊 Aggregated {} candles", processed_candles.len());
            for candle in processed_candles {
                let msg = json!({
                    "type": "candle",
                    "data": candle
                })
                .to_string();
                let _ = tx_broadcast.send(msg);
            }
        }

        // Update local state
        for (name, _) in config::INTERVALS {
            last_processed.insert(name.to_string(), current_time);
        }
    }
}
