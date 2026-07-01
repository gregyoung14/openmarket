use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BinanceTick {
    pub source_ts_ms: i64,
    pub ingest_ts_ms: i64,
    pub trade_time_ms: i64,
    pub price: f64,
    pub volume: f64,
    pub raw_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PolymarketTick {
    pub source_ts_ms: i64,
    pub ingest_ts_ms: i64,
    pub market_slug: Option<String>,
    pub asset_id: String,
    pub side_label: String,
    pub event_type: String,
    pub price: Option<f64>,
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
    pub size: Option<f64>,
    pub raw_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarketMeta {
    pub market_slug: String,
    pub question: String,
    pub up_token_id: String,
    pub down_token_id: String,
    pub up_price: f64,
    pub down_price: f64,
    pub first_seen_ms: i64,
    pub last_seen_ms: i64,
}

#[derive(Debug, Clone)]
pub struct PolyUnpaired {
    pub id: i64,
    pub source_ts_ms: i64,
    pub market_slug: Option<String>,
    pub side_label: String,
    pub best_bid: f64,
}

#[derive(Debug, Clone)]
pub struct NearestBinance {
    pub id: i64,
    pub source_ts_ms: i64,
    pub price: f64,
}

#[derive(Debug, Clone)]
pub enum DbMessage {
    Binance(BinanceTick),
    Polymarket(PolymarketTick),
    MarketMeta(MarketMeta),
}
