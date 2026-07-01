use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use tokio::sync::broadcast;

/// Shared application state
#[derive(Clone)]
pub struct AppState {
    pub tx: broadcast::Sender<String>,
    binance_ws_connected: Arc<AtomicBool>,
    last_upstream_trade_received_ms: Arc<AtomicI64>,
    last_trade_broadcast_ms: Arc<AtomicI64>,
}

impl AppState {
    pub fn new(tx: broadcast::Sender<String>) -> Self {
        Self {
            tx,
            binance_ws_connected: Arc::new(AtomicBool::new(false)),
            last_upstream_trade_received_ms: Arc::new(AtomicI64::new(0)),
            last_trade_broadcast_ms: Arc::new(AtomicI64::new(0)),
        }
    }

    pub fn set_binance_ws_connected(&self, connected: bool) {
        self.binance_ws_connected
            .store(connected, Ordering::Relaxed);
    }

    pub fn binance_ws_connected(&self) -> bool {
        self.binance_ws_connected.load(Ordering::Relaxed)
    }

    pub fn record_upstream_trade_received(&self, timestamp_ms: i64) {
        self.last_upstream_trade_received_ms
            .store(timestamp_ms, Ordering::Relaxed);
    }

    pub fn record_trade_broadcast(&self, timestamp_ms: i64) {
        self.last_trade_broadcast_ms
            .store(timestamp_ms, Ordering::Relaxed);
    }

    pub fn last_upstream_trade_received_ms(&self) -> Option<i64> {
        let timestamp_ms = self.last_upstream_trade_received_ms.load(Ordering::Relaxed);
        (timestamp_ms > 0).then_some(timestamp_ms)
    }

    pub fn last_trade_broadcast_ms(&self) -> Option<i64> {
        let timestamp_ms = self.last_trade_broadcast_ms.load(Ordering::Relaxed);
        (timestamp_ms > 0).then_some(timestamp_ms)
    }
}
