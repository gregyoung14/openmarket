use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trade {
    pub trade_id: i64,
    pub trade_time: i64,
    pub price: f64,
    pub quantity: f64,
    pub quote_volume: f64,
    pub is_buyer_maker: i32,
    pub received_at: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Candle {
    pub interval: String,
    pub time: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}
