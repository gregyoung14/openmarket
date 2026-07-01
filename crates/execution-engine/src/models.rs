use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

// ─── Signal Engine Messages (inbound from ws://127.0.0.1:8003/ws) ─────────

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
#[allow(dead_code)]
pub enum SignalMessage {
    Connected {
        service: Option<String>,
        #[serde(default)]
        timestamp: Option<i64>,
    },
    Ready {
        features: Option<u32>,
    },
    Prediction(Prediction),
    Entry(Box<EntrySignal>),
    Exit(ExitSignal),
    MarketInfo(MarketInfoMsg),
    #[serde(rename = "new_market")]
    NewMarket(MarketInfoMsg),
    PriceChange(PriceChangeMsg),
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct Prediction {
    pub direction: String,
    pub confidence: f64,
    pub raw_prob: f64,
    pub timestamp: Option<i64>,
    pub market: Option<String>,
    pub secs_in: Option<i64>,
    pub secs_left: Option<i64>,
    pub n: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct EntrySignal {
    pub direction: String,
    pub confidence: f64,
    #[serde(default)]
    pub consistency: Option<f64>,
    #[serde(default)]
    pub raw_prob: Option<f64>,
    #[serde(default)]
    pub combined_prob_up: Option<f64>,
    #[serde(default)]
    pub drift_prob_up: Option<f64>,
    #[serde(default)]
    pub market: Option<String>,
    #[serde(default)]
    pub secs_in: Option<u64>,
    #[serde(default)]
    pub secs_left: Option<u64>,
    #[serde(default)]
    pub entry_ask: Option<f64>,
    #[serde(default)]
    pub entry_bid: Option<f64>,
    #[serde(default)]
    pub btc_price: Option<f64>,
    #[serde(default)]
    pub n_trades: Option<usize>,
    #[serde(default)]
    pub edge: Option<f64>,
    #[serde(default)]
    pub regime: Option<String>,
    #[serde(default)]
    pub path_eff: Option<f64>,
    #[serde(default)]
    pub autocorr: Option<f64>,
    #[serde(default)]
    pub ofi_accel: Option<f64>,
    #[serde(default)]
    pub adaptive_confirm: Option<u64>,
    #[serde(default)]
    pub vol_1s: Option<f64>,
    #[serde(default)]
    pub timestamp: Option<i64>,
    #[serde(default)]
    pub version: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct ExitSignal {
    pub market: Option<String>,
    pub up_bid: Option<f64>,
    pub down_bid: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct MarketInfoMsg {
    pub slug: Option<String>,
    pub question: Option<String>,
    #[serde(default, alias = "assets_ids", alias = "clobTokenIds")]
    pub token_ids: Option<Vec<String>>,
    pub end_date: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct PriceChangeMsg {
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
    pub token_id: Option<String>,
    pub asset_id: Option<String>,
    pub side: Option<String>,
    /// Resolved UP/DOWN side from signal engine (Fix 4)
    #[serde(default)]
    pub market_side: Option<String>,
}

// ─── Polymarket WS Messages (inbound from ws://127.0.0.1:8002/ws) ─────────

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
#[allow(dead_code)]
pub enum PolymarketMessage {
    Connected {
        #[serde(default)]
        service: Option<String>,
    },
    MarketInfo {
        slug: Option<String>,
        question: Option<String>,
        #[serde(default, alias = "assets_ids", alias = "clobTokenIds")]
        token_ids: Option<Vec<String>>,
        #[serde(default)]
        end_date: Option<String>,
    },
    #[serde(rename = "new_market")]
    NewMarket {
        slug: Option<String>,
        question: Option<String>,
        #[serde(default, alias = "token_ids", alias = "clobTokenIds")]
        token_ids: Option<Vec<String>>,
        #[serde(default)]
        end_date: Option<String>,
    },
    PriceChange {
        best_bid: Option<f64>,
        best_ask: Option<f64>,
        price: Option<f64>,
        token_id: Option<String>,
        asset_id: Option<String>,
        side: Option<String>,
        /// Resolved UP/DOWN side from polymarket-websocket (Fix 4)
        #[serde(default)]
        market_side: Option<String>,
    },
    Book {
        asset_id: Option<String>,
        bids: Option<Vec<Vec<serde_json::Value>>>,
        asks: Option<Vec<Vec<serde_json::Value>>>,
        /// Resolved UP/DOWN side from polymarket-websocket (Fix 4)
        #[serde(default)]
        side: Option<String>,
        /// Resolved UP/DOWN side (used by price_change messages)
        #[serde(default)]
        market_side: Option<String>,
    },
    #[serde(rename = "market_resolved")]
    MarketResolved {
        #[serde(default)]
        winning_asset_id: Option<String>,
        #[serde(default)]
        winning_outcome: Option<String>,
    },
    #[serde(other)]
    Unknown,
}

// ─── Internal Domain Types ─────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Direction {
    Up,
    Down,
}

impl Direction {
    pub fn from_str_loose(s: &str) -> Option<Self> {
        match s.to_uppercase().as_str() {
            "UP" => Some(Self::Up),
            "DOWN" => Some(Self::Down),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Up => "UP",
            Self::Down => "DOWN",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExitStrategy {
    HoldToResolve,
    Momentum,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExitType {
    ResolveWin,
    ResolveLoss,
    TakeProfit,
    StopLoss,
    ManualClose,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub id: String,
    pub market_slug: String,
    pub side: Direction,
    pub token_id: String,
    pub entry_price: f64,
    pub shares: f64,
    pub bet_amount: f64,
    pub confidence: f64,
    pub consistency: f64,
    pub entry_time: DateTime<Utc>,
    pub market_end_ms: i64,
    pub strategy: ExitStrategy,
    // Filled on close
    pub exit_price: Option<f64>,
    pub exit_time: Option<DateTime<Utc>>,
    pub exit_type: Option<ExitType>,
    pub pnl: Option<f64>,
}

/// Live market price state
#[derive(Debug, Clone, Default)]
pub struct LivePrices {
    pub up_bid: Option<f64>,
    pub up_ask: Option<f64>,
    pub down_bid: Option<f64>,
    pub down_ask: Option<f64>,
}

/// Current market context
#[derive(Debug, Clone)]
pub struct MarketContext {
    pub slug: String,
    pub up_token_id: String,
    pub down_token_id: String,
    pub market_end_ms: i64,
}

// ─── Status Sub-structs (included in Status event) ─────────────────────────

#[derive(Debug, Clone, Serialize, Default)]
pub struct StatusPrices {
    pub up_bid: Option<f64>,
    pub up_ask: Option<f64>,
    pub down_bid: Option<f64>,
    pub down_ask: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct StatusWallet {
    pub usdc_e: f64,
    pub usdc_native: f64,
    pub matic: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusPosition {
    pub id: String,
    pub side: String,
    pub entry_price: f64,
    pub shares: f64,
    pub bet_amount: f64,
    pub confidence: f64,
    pub entry_time: String,
    pub unrealized_pnl: f64,
}

// ─── Execution Events (broadcast to WS clients) ───────────────────────────

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ExecutionEvent {
    Status {
        bankroll: f64,
        peak_bankroll: f64,
        open_positions: usize,
        total_trades: usize,
        wins: usize,
        win_rate: f64,
        total_pnl: f64,
        drawdown_pct: f64,
        strategy: String,
        clob_connected: bool,
        wallet_address: String,
        uptime_secs: u64,
        market_slug: String,
        prices: StatusPrices,
        wallet_balances: StatusWallet,
        positions: Vec<StatusPosition>,
        timestamp: i64,
    },
    TradeOpened {
        position_id: String,
        market: String,
        side: String,
        entry_price: f64,
        shares: f64,
        bet_amount: f64,
        confidence: f64,
        timestamp: i64,
    },
    TradeClosed {
        position_id: String,
        market: String,
        side: String,
        entry_price: f64,
        exit_price: f64,
        shares: f64,
        pnl: f64,
        exit_type: String,
        timestamp: i64,
    },
    Signal {
        direction: String,
        confidence: f64,
        consistency: f64,
        n_predictions: usize,
        market: String,
        secs_in: i64,
        action: String, // "WATCHING", "ENTERING", "BLOCKED"
        timestamp: i64,
    },
    Error {
        message: String,
        timestamp: i64,
    },
}

// ─── Unit Tests ────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Guard: if someone bumps SIGNAL_VERSION in btc-common but forgets
    /// to update the raw JSON test fixtures in this file, this will fail.
    #[test]
    fn test_fixtures_match_current_version() {
        assert_eq!(
            btc_common::version::SIGNAL_VERSION,
            "v8-fix-validation",
            "Test JSON fixtures contain hardcoded version strings. \
             Update them when SIGNAL_VERSION changes."
        );
    }

    /// The exact JSON the signal engine broadcasts for an entry signal.
    const ENTRY_JSON: &str = r#"{
        "type": "entry",
        "direction": "UP",
        "confidence": 0.7885833792234667,
        "consistency": 1.0,
        "raw_prob": 0.7885833792234667,
        "combined_prob_up": 0.7885833792234667,
        "drift_prob_up": 0.9999999325215542,
        "market": "btc-updown-15m-1771695900",
        "secs_in": 110,
        "secs_left": 790,
        "entry_ask": 0.46,
        "entry_bid": 0.45,
        "btc_price": 68418.2,
        "n_trades": 374,
        "edge": 0.32358337922346664,
        "regime": "trend",
        "path_eff": 0.8168187744467514,
        "autocorr": 0.2538765106686418,
        "ofi_accel": 3.939887860404667e-9,
        "adaptive_confirm": 38,
        "vol_1s": 0.000015272590503208453,
        "timestamp": 1771696010648,
        "version": "v8-fix-validation"
    }"#;

    const PREDICTION_JSON: &str = r#"{
        "type": "prediction",
        "direction": "DOWN",
        "confidence": 0.65,
        "raw_prob": 0.35,
        "market": "btc-updown-15m-1771695000",
        "secs_in": 120,
        "secs_left": 780,
        "n": 500,
        "timestamp": 1771695120000
    }"#;

    const CONNECTED_JSON: &str = r#"{
        "type": "connected",
        "service": "signal-engine",
        "timestamp": 1771695000000
    }"#;

    const MARKET_INFO_JSON: &str = r#"{
        "type": "market_info",
        "slug": "btc-updown-15m-1771695900",
        "question": "Will BTC go up?",
        "token_ids": ["token_up_123", "token_down_456"],
        "end_date": "2026-02-27T18:00:00Z"
    }"#;

    // ── EntrySignal deserialization ──────────────────────────────────

    #[test]
    fn test_entry_signal_parses_from_signal_engine_json() {
        let msg: SignalMessage = serde_json::from_str(ENTRY_JSON).unwrap();
        match msg {
            SignalMessage::Entry(entry) => {
                assert_eq!(entry.direction, "UP");
                assert!((entry.confidence - 0.7886).abs() < 0.001);
                assert_eq!(entry.consistency, Some(1.0));
                assert_eq!(entry.entry_ask, Some(0.46));
                assert_eq!(entry.entry_bid, Some(0.45));
                assert_eq!(entry.market, Some("btc-updown-15m-1771695900".to_string()));
                assert_eq!(entry.secs_in, Some(110));
                assert_eq!(entry.secs_left, Some(790));
                assert!((entry.edge.unwrap() - 0.3236).abs() < 0.001);
                assert_eq!(entry.regime, Some("trend".to_string()));
                assert_eq!(entry.adaptive_confirm, Some(38));
                assert_eq!(entry.version, Some("v8-fix-validation".to_string()));
            }
            other => panic!("Expected Entry, got {other:?}"),
        }
    }

    #[test]
    fn test_entry_signal_without_optional_fields() {
        // Minimal entry signal — only required fields
        let json = r#"{
            "type": "entry",
            "direction": "DOWN",
            "confidence": 0.72
        }"#;
        let msg: SignalMessage = serde_json::from_str(json).unwrap();
        match msg {
            SignalMessage::Entry(entry) => {
                assert_eq!(entry.direction, "DOWN");
                assert!((entry.confidence - 0.72).abs() < 0.001);
                assert_eq!(entry.consistency, None);
                assert_eq!(entry.entry_ask, None);
                assert_eq!(entry.market, None);
                assert_eq!(entry.regime, None);
                assert_eq!(entry.edge, None);
                assert_eq!(entry.version, None);
            }
            other => panic!("Expected Entry, got {other:?}"),
        }
    }

    #[test]
    fn test_entry_signal_no_side_field_required() {
        // The old format required "side" which caused 258 parse failures.
        // Verify that entry signals work WITHOUT a "side" field.
        let json = r#"{
            "type": "entry",
            "direction": "UP",
            "confidence": 0.65,
            "entry_ask": 0.50,
            "market": "btc-updown-15m-1771695000"
        }"#;
        let result = serde_json::from_str::<SignalMessage>(json);
        assert!(
            result.is_ok(),
            "Entry signal should parse without 'side' field: {result:?}"
        );
    }

    // ── Prediction deserialization ───────────────────────────────────

    #[test]
    fn test_prediction_parses_correctly() {
        let msg: SignalMessage = serde_json::from_str(PREDICTION_JSON).unwrap();
        match msg {
            SignalMessage::Prediction(pred) => {
                assert_eq!(pred.direction, "DOWN");
                assert!((pred.confidence - 0.65).abs() < 0.001);
                assert!((pred.raw_prob - 0.35).abs() < 0.001);
                assert_eq!(pred.market, Some("btc-updown-15m-1771695000".to_string()));
                assert_eq!(pred.secs_in, Some(120));
            }
            other => panic!("Expected Prediction, got {other:?}"),
        }
    }

    // ── Connected / MarketInfo dispatch ──────────────────────────────

    #[test]
    fn test_connected_message_parses() {
        let msg: SignalMessage = serde_json::from_str(CONNECTED_JSON).unwrap();
        assert!(matches!(msg, SignalMessage::Connected { .. }));
    }

    #[test]
    fn test_market_info_parses() {
        let msg: SignalMessage = serde_json::from_str(MARKET_INFO_JSON).unwrap();
        match msg {
            SignalMessage::MarketInfo(info) => {
                assert_eq!(info.slug, Some("btc-updown-15m-1771695900".to_string()));
                let token_ids = info.token_ids.unwrap();
                assert_eq!(token_ids.len(), 2);
                assert_eq!(token_ids[0], "token_up_123");
                assert_eq!(token_ids[1], "token_down_456");
            }
            other => panic!("Expected MarketInfo, got {other:?}"),
        }
    }

    // ── Unknown types fall through gracefully ───────────────────────

    #[test]
    fn test_unknown_type_does_not_error() {
        let json = r#"{"type": "something_new", "data": 123}"#;
        let msg: SignalMessage = serde_json::from_str(json).unwrap();
        assert!(matches!(msg, SignalMessage::Unknown));
    }

    // ── Direction parsing ───────────────────────────────────────────

    #[test]
    fn test_direction_from_str_loose() {
        assert_eq!(Direction::from_str_loose("UP"), Some(Direction::Up));
        assert_eq!(Direction::from_str_loose("up"), Some(Direction::Up));
        assert_eq!(Direction::from_str_loose("Down"), Some(Direction::Down));
        assert_eq!(Direction::from_str_loose("DOWN"), Some(Direction::Down));
        assert_eq!(Direction::from_str_loose("invalid"), None);
        assert_eq!(Direction::from_str_loose(""), None);
    }

    #[test]
    fn test_direction_as_str() {
        assert_eq!(Direction::Up.as_str(), "UP");
        assert_eq!(Direction::Down.as_str(), "DOWN");
    }

    // ── Regression: old format with "side" still parses (extra field ignored) ──

    #[test]
    fn test_entry_with_extra_side_field_still_works() {
        let json = r#"{
            "type": "entry",
            "side": "UP",
            "direction": "UP",
            "confidence": 0.70,
            "entry_ask": 0.45
        }"#;
        let msg: SignalMessage = serde_json::from_str(json).unwrap();
        match msg {
            SignalMessage::Entry(entry) => {
                assert_eq!(entry.direction, "UP");
                assert!((entry.confidence - 0.70).abs() < 0.001);
            }
            other => panic!("Expected Entry, got {other:?}"),
        }
    }

    // ── Full roundtrip: real signal engine output ───────────────────

    #[test]
    fn test_real_signal_engine_entry_from_logs() {
        // This is a real entry signal that was previously failing with:
        // "missing field `side`"
        let json = r#"{"type":"entry","direction":"UP","confidence":0.6571408095543267,"consistency":1.0,"raw_prob":0.6571408095543267,"combined_prob_up":0.6571408095543267,"drift_prob_up":0.771366369086395,"market":"btc-updown-15m-1771695000","secs_in":258,"secs_left":642,"entry_ask":0.51,"entry_bid":0.5,"btc_price":68303.01,"n_trades":1391,"timestamp":1771695258829,"edge":0.14214080955432673,"regime":"trend","path_eff":0.9922507925365747,"autocorr":-0.03686739418440255,"ofi_accel":1.5267429542831223e-9,"adaptive_confirm":38,"vol_1s":8.851907830888351e-8,"version":"v14"}"#;

        let msg: SignalMessage = serde_json::from_str(json).unwrap();
        match msg {
            SignalMessage::Entry(entry) => {
                assert_eq!(entry.direction, "UP");
                assert!((entry.confidence - 0.6571).abs() < 0.001);
                assert_eq!(entry.entry_ask, Some(0.51));
                assert_eq!(entry.n_trades, Some(1391));
                assert_eq!(entry.regime, Some("trend".to_string()));
            }
            other => panic!("Expected Entry, got {other:?}"),
        }
    }
}
