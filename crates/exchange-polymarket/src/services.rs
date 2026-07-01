use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use tokio::sync::broadcast;

#[derive(Clone)]
pub struct AppState {
    pub broadcast_tx: broadcast::Sender<String>,
    pub current_market: std::sync::Arc<parking_lot::Mutex<Option<String>>>,
    pub current_market_message: std::sync::Arc<parking_lot::Mutex<Option<String>>>,
    /// Token ID → side ("UP" or "DOWN") mapping for the current market
    pub token_side_map: std::sync::Arc<parking_lot::Mutex<HashMap<String, String>>>,
    upstream_connected: std::sync::Arc<AtomicBool>,
    last_upstream_message_ms: std::sync::Arc<AtomicI64>,
    last_market_data_ms: std::sync::Arc<AtomicI64>,
    last_market_change_ms: std::sync::Arc<AtomicI64>,
}

impl AppState {
    pub fn new(broadcast_tx: broadcast::Sender<String>) -> Self {
        Self {
            broadcast_tx,
            current_market: std::sync::Arc::new(parking_lot::Mutex::new(None)),
            current_market_message: std::sync::Arc::new(parking_lot::Mutex::new(None)),
            token_side_map: std::sync::Arc::new(parking_lot::Mutex::new(HashMap::new())),
            upstream_connected: std::sync::Arc::new(AtomicBool::new(false)),
            last_upstream_message_ms: std::sync::Arc::new(AtomicI64::new(0)),
            last_market_data_ms: std::sync::Arc::new(AtomicI64::new(0)),
            last_market_change_ms: std::sync::Arc::new(AtomicI64::new(0)),
        }
    }

    pub fn set_market(&self, market_id: String) {
        *self.current_market.lock() = Some(market_id);
        let now_ms = Self::now_ms();
        self.last_market_change_ms.store(now_ms, Ordering::Relaxed);
        self.last_market_data_ms.store(now_ms, Ordering::Relaxed);
    }

    #[allow(dead_code)]
    pub fn get_market(&self) -> Option<String> {
        self.current_market.lock().clone()
    }

    pub fn set_market_message(&self, msg: String) {
        *self.current_market_message.lock() = Some(msg);
    }

    pub fn get_market_message(&self) -> Option<String> {
        self.current_market_message.lock().clone()
    }

    /// Set token_ids[0] = UP, token_ids[1] = DOWN (already ordered by fetch_active_btc_market)
    pub fn set_token_sides(&self, token_ids: &[String]) {
        let mut map = self.token_side_map.lock();
        map.clear();
        if token_ids.len() >= 2 {
            map.insert(token_ids[0].clone(), "UP".to_string());
            map.insert(token_ids[1].clone(), "DOWN".to_string());
        }
    }

    /// Resolve asset_id → "UP" or "DOWN"
    pub fn get_side_for_token(&self, asset_id: &str) -> Option<String> {
        self.token_side_map.lock().get(asset_id).cloned()
    }

    pub fn set_upstream_connected(&self, connected: bool) {
        self.upstream_connected.store(connected, Ordering::Relaxed);
    }

    pub fn upstream_connected(&self) -> bool {
        self.upstream_connected.load(Ordering::Relaxed)
    }

    pub fn record_upstream_message(&self, timestamp_ms: i64) {
        self.last_upstream_message_ms
            .store(timestamp_ms, Ordering::Relaxed);
    }

    pub fn record_market_data(&self, timestamp_ms: i64) {
        self.last_market_data_ms
            .store(timestamp_ms, Ordering::Relaxed);
    }

    pub fn last_upstream_message_ms(&self) -> Option<i64> {
        let timestamp_ms = self.last_upstream_message_ms.load(Ordering::Relaxed);
        (timestamp_ms > 0).then_some(timestamp_ms)
    }

    pub fn last_market_data_ms(&self) -> Option<i64> {
        let timestamp_ms = self.last_market_data_ms.load(Ordering::Relaxed);
        (timestamp_ms > 0).then_some(timestamp_ms)
    }

    pub fn last_market_change_ms(&self) -> Option<i64> {
        let timestamp_ms = self.last_market_change_ms.load(Ordering::Relaxed);
        (timestamp_ms > 0).then_some(timestamp_ms)
    }

    fn now_ms() -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    }
}
