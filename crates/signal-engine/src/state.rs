use crate::calibrated::{BinaryModelArtifact, ScorerMode};
use crate::models::{BinanceTrade, ConfirmationState, EngineStats, MarketInfo};
use crate::volume::VolumeMedianEstimator;
use btc_common::version;
use parking_lot::Mutex;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::broadcast;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

#[derive(Debug, Clone)]
pub struct BestEntryCandidate {
    pub direction: String,
    pub confidence: f64,
    pub consistency: f64,
    pub combined_prob_up: f64,
    pub drift_prob_up: f64,
    pub regime: String,
    pub path_eff: f64,
    pub autocorr: f64,
    pub ofi_accel: f64,
    pub adaptive_confirm: u64,
    pub vol_1s: f64,
    pub edge: f64,
    pub ranking_score: f64,
    pub ranking_basis: String,
    pub entry_ask: f64,
    pub entry_bid: f64,
    pub secs_in: u64,
    pub n_trades: usize,
    pub scoring_mode: String,
    pub raw_model_prob_up: Option<f64>,
    pub calibrated_prob_up: Option<f64>,
    pub selected_side_prob: Option<f64>,
    pub ev_up: Option<f64>,
    pub ev_down: Option<f64>,
    pub artifact_version: Option<String>,
}

/// Shared application state for Signal Engine v4.1 (v9.2 regime-aware)
#[derive(Clone)]
pub struct AppState {
    /// Broadcast channel for downstream WS clients (execution engine)
    pub signal_tx: broadcast::Sender<String>,
    /// Engine statistics
    pub stats: Arc<Mutex<EngineStats>>,
    /// Service start time
    pub start_time: Instant,

    // ── Market tracking ──
    /// Current market info from Polymarket
    pub current_market: Arc<Mutex<Option<MarketInfo>>>,
    /// Token ID → side mapping (asset_id → "UP" or "DOWN")
    pub token_side_map: Arc<Mutex<HashMap<String, String>>>,

    // ── Trade buffer ──
    /// Binance trades for the current market window (cleared on new market)
    pub trade_buffer: Arc<Mutex<Vec<BinanceTrade>>>,
    /// BTC price at market open (first trade after market_info)
    pub open_price: Arc<Mutex<Option<f64>>>,

    // ── Confirmation state ──
    pub confirmation: Arc<Mutex<ConfirmationState>>,
    /// Whether we've already fired an entry for this market
    pub entry_fired: Arc<Mutex<bool>>,
    /// Best qualified signal candidate in this market window
    pub best_candidate: Arc<Mutex<Option<BestEntryCandidate>>>,
    /// Rolling volume estimator for v14 volume gate
    pub volume_estimator: Arc<Mutex<VolumeMedianEstimator>>,
    /// Optional calibrated paper scorer artifact
    pub calibrated_model: Option<Arc<BinaryModelArtifact>>,
    /// Effective calibrated scorer mode
    pub scorer_mode: ScorerMode,
}

impl AppState {
    #[allow(dead_code)]
    pub fn new(signal_tx: broadcast::Sender<String>) -> Self {
        Self::new_with_calibrated(signal_tx, None, ScorerMode::Disabled)
    }

    pub fn new_with_calibrated(
        signal_tx: broadcast::Sender<String>,
        calibrated_model: Option<Arc<BinaryModelArtifact>>,
        scorer_mode: ScorerMode,
    ) -> Self {
        Self {
            signal_tx,
            stats: Arc::new(Mutex::new(EngineStats {
                version: version::SIGNAL_VERSION.to_string(),
                calibrated_mode: scorer_mode.as_str().to_string(),
                calibrated_loaded: calibrated_model.is_some(),
                calibrated_artifact_version: calibrated_model
                    .as_ref()
                    .map(|artifact| artifact.artifact_label()),
                ..Default::default()
            })),
            start_time: Instant::now(),
            current_market: Arc::new(Mutex::new(None)),
            token_side_map: Arc::new(Mutex::new(HashMap::new())),
            trade_buffer: Arc::new(Mutex::new(Vec::with_capacity(10_000))),
            open_price: Arc::new(Mutex::new(None)),
            confirmation: Arc::new(Mutex::new(ConfirmationState::default())),
            entry_fired: Arc::new(Mutex::new(false)),
            best_candidate: Arc::new(Mutex::new(None)),
            volume_estimator: Arc::new(Mutex::new(VolumeMedianEstimator::with_capacity(
                crate::config::VOLUME_MEDIAN_OBSERVATIONS,
            ))),
            calibrated_model,
            scorer_mode,
        }
    }

    pub fn update_stats(&self, f: impl FnOnce(&mut EngineStats)) {
        let mut stats = self.stats.lock();
        f(&mut stats);
        stats.uptime_secs = self.start_time.elapsed().as_secs();
    }

    pub fn get_stats(&self) -> EngineStats {
        let mut stats = self.stats.lock().clone();
        stats.uptime_secs = self.start_time.elapsed().as_secs();
        stats
    }

    /// Called when a new market is detected — resets trade buffer and confirmation
    pub fn new_market(&self, market: MarketInfo) {
        let previous_volume: f64 = self.trade_buffer.lock().iter().map(|t| t.quantity).sum();
        if previous_volume > 0.0 {
            let hourly_rate =
                previous_volume / (crate::config::MARKET_DURATION_SECS as f64 / 3600.0);
            self.volume_estimator.lock().observe(hourly_rate);
        }

        let slug = market.slug.clone();
        let start_ms = market.start_ms;

        *self.current_market.lock() = Some(market);
        self.trade_buffer.lock().clear();
        *self.open_price.lock() = None;
        self.confirmation.lock().reset();
        *self.entry_fired.lock() = false;
        *self.best_candidate.lock() = None;

        let current_time_ms = now_ms();

        self.update_stats(|s| {
            s.current_market = Some(slug);
            s.market_start_ms = Some(start_ms);
            s.confirmation_count = 0;
            s.confirmation_direction = None;
            s.last_market_info_time = Some(current_time_ms);
            s.last_polymarket_data_time = Some(current_time_ms);
        });
    }

    /// Add a Binance trade to the buffer
    pub fn push_trade(&self, trade: BinanceTrade) {
        // Set open price from first trade after market start
        let market_start = self.current_market.lock().as_ref().map(|m| m.start_ms);
        if let Some(start_ms) = market_start {
            if trade.trade_time_ms >= start_ms {
                let mut op = self.open_price.lock();
                if op.is_none() {
                    *op = Some(trade.price);
                    tracing::info!("📊 BTC open price set: {:.2}", trade.price);
                }
            }
        }

        let current_time_ms = now_ms();

        self.update_stats(|s| {
            s.last_btc_price = Some(trade.price);
            s.binance_trades_total += 1;
            s.last_binance_trade_time = Some(current_time_ms);
        });

        self.trade_buffer.lock().push(trade);
        self.update_stats(|s| s.binance_trades_buffered = self.trade_buffer.lock().len() as u64);
    }

    /// Update Polymarket best bid/ask for a given side
    pub fn update_poly_price(&self, side: &str, best_bid: f64, best_ask: f64) {
        let mut market = self.current_market.lock();
        if let Some(ref mut m) = *market {
            match side {
                "UP" => {
                    m.up_best_bid = best_bid;
                    m.up_best_ask = best_ask;
                    m.up_price = (best_bid + best_ask) / 2.0;
                }
                "DOWN" => {
                    m.down_best_bid = best_bid;
                    m.down_best_ask = best_ask;
                    m.down_price = (best_bid + best_ask) / 2.0;
                }
                _ => {}
            }
        }
    }

    pub fn get_market(&self) -> Option<MarketInfo> {
        self.current_market.lock().clone()
    }

    pub fn get_open_price(&self) -> Option<f64> {
        *self.open_price.lock()
    }

    pub fn get_trades_snapshot(&self) -> Vec<BinanceTrade> {
        self.trade_buffer.lock().clone()
    }

    /// Map token IDs to UP/DOWN sides
    pub fn set_token_sides(&self, token_ids: &[String]) {
        let mut map = self.token_side_map.lock();
        map.clear();
        if token_ids.len() >= 2 {
            map.insert(token_ids[0].clone(), "UP".to_string());
            map.insert(token_ids[1].clone(), "DOWN".to_string());
            tracing::info!(
                "Token mapping: {} → UP, {} → DOWN",
                &token_ids[0][..8.min(token_ids[0].len())],
                &token_ids[1][..8.min(token_ids[1].len())]
            );
        }
    }

    pub fn get_side_for_token(&self, asset_id: &str) -> Option<String> {
        self.token_side_map.lock().get(asset_id).cloned()
    }

    pub fn volume_median(&self) -> Option<f64> {
        let estimator = self.volume_estimator.lock();
        if estimator.len() >= crate::config::MIN_VOLUME_OBSERVATIONS {
            Some(estimator.median())
        } else {
            None
        }
    }

    pub fn consider_best_candidate(&self, candidate: BestEntryCandidate) -> bool {
        let mut best = self.best_candidate.lock();
        let should_replace = best
            .as_ref()
            .map(|current| {
                const EPS: f64 = 1e-9;

                if candidate.ranking_score > current.ranking_score + EPS {
                    true
                } else if (candidate.ranking_score - current.ranking_score).abs() <= EPS {
                    if candidate.confidence > current.confidence + EPS {
                        true
                    } else if (candidate.confidence - current.confidence).abs() <= EPS {
                        candidate.entry_ask < current.entry_ask - EPS
                    } else {
                        false
                    }
                } else {
                    false
                }
            })
            .unwrap_or(true);
        if should_replace {
            *best = Some(candidate);
            true
        } else {
            false
        }
    }

    pub fn take_best_candidate(&self) -> Option<BestEntryCandidate> {
        self.best_candidate.lock().take()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate(confidence: f64, edge: f64, entry_ask: f64) -> BestEntryCandidate {
        BestEntryCandidate {
            direction: "UP".to_string(),
            confidence,
            consistency: 1.0,
            combined_prob_up: confidence,
            drift_prob_up: confidence,
            regime: "trend".to_string(),
            path_eff: 0.5,
            autocorr: 0.0,
            ofi_accel: 0.0,
            adaptive_confirm: 30,
            vol_1s: 0.0,
            edge,
            ranking_score: edge,
            ranking_basis: "edge".to_string(),
            entry_ask,
            entry_bid: entry_ask - 0.01,
            secs_in: 120,
            n_trades: 100,
            scoring_mode: "disabled".to_string(),
            raw_model_prob_up: None,
            calibrated_prob_up: None,
            selected_side_prob: None,
            ev_up: None,
            ev_down: None,
            artifact_version: None,
        }
    }

    #[test]
    fn best_candidate_prefers_higher_edge() {
        let state = AppState::new(tokio::sync::broadcast::channel(8).0);
        assert!(state.consider_best_candidate(candidate(0.99, 0.12, 0.53)));
        assert!(state.consider_best_candidate(candidate(0.94, 0.18, 0.49)));

        let best = state.take_best_candidate().unwrap();
        assert!((best.edge - 0.18).abs() < 1e-9);
        assert!((best.entry_ask - 0.49).abs() < 1e-9);
    }

    #[test]
    fn best_candidate_breaks_ties_with_lower_price() {
        let state = AppState::new(tokio::sync::broadcast::channel(8).0);
        assert!(state.consider_best_candidate(candidate(0.90, 0.10, 0.52)));
        assert!(state.consider_best_candidate(candidate(0.90, 0.10, 0.47)));

        let best = state.take_best_candidate().unwrap();
        assert!((best.entry_ask - 0.47).abs() < 1e-9);
    }
}
