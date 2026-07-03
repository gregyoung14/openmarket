use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct BookUpdate {
    pub asset_id: String,
    pub bids: Vec<[f64; 2]>, // [price, size]
    pub asks: Vec<[f64; 2]>,
    pub timestamp: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct PriceChange {
    pub asset_id: String,
    pub price: f64,
    pub size: f64,
    pub side: String, // "BUY" or "SELL"
    pub timestamp: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct TradeUpdate {
    pub asset_id: String,
    pub price: f64,
    pub size: f64,
    pub side: String,
    pub timestamp: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct Market {
    pub id: String,
    pub slug: String,
    pub token_ids: Vec<String>,
    pub question: String,
}

#[derive(Debug, Clone, Serialize)]
#[allow(dead_code)]
pub struct MarketSnapshot {
    pub timestamp: i64,
    pub source: String,
    pub market_id: String,
    pub bids: Option<Vec<[f64; 2]>>,
    pub asks: Option<Vec<[f64; 2]>>,
}
