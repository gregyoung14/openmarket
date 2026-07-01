use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::{mpsc, RwLock};

use crate::models::{DbMessage, MarketMeta};

/// Side + market slug info for a single asset_id
#[derive(Clone, Debug)]
pub struct AssetInfo {
    pub side: String, // "UP" or "DOWN"
    pub market_slug: String,
}

#[derive(Clone, Default)]
pub struct TokenMapping {
    pub market_slug: Option<String>,
    pub up_token_id: Option<String>,
    pub down_token_id: Option<String>,
    /// Persistent map: asset_id → (side, market_slug)
    /// Survives across market transitions so we never lose track
    pub asset_map: HashMap<String, AssetInfo>,
}

#[derive(Clone)]
pub struct AppState {
    pub db_tx: mpsc::Sender<DbMessage>,
    pub token_mapping: Arc<RwLock<TokenMapping>>,
    pub binance_ingested: Arc<AtomicU64>,
    pub polymarket_ingested: Arc<AtomicU64>,
    pub lag_pairs_written: Arc<AtomicU64>,
    pub last_market: Arc<RwLock<Option<MarketMeta>>>,
}

impl AppState {
    pub fn new(db_tx: mpsc::Sender<DbMessage>) -> Self {
        Self {
            db_tx,
            token_mapping: Arc::new(RwLock::new(TokenMapping::default())),
            binance_ingested: Arc::new(AtomicU64::new(0)),
            polymarket_ingested: Arc::new(AtomicU64::new(0)),
            lag_pairs_written: Arc::new(AtomicU64::new(0)),
            last_market: Arc::new(RwLock::new(None)),
        }
    }

    pub fn inc_binance(&self) {
        self.binance_ingested.fetch_add(1, Ordering::Relaxed);
    }

    pub fn inc_polymarket(&self) {
        self.polymarket_ingested.fetch_add(1, Ordering::Relaxed);
    }

    pub fn inc_lag_pairs(&self, by: u64) {
        self.lag_pairs_written.fetch_add(by, Ordering::Relaxed);
    }
}
