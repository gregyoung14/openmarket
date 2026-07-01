use std::fs;

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};

use crate::models::{BinanceTrade, DriftSignal, MarketInfo, OneSecondBars, Regime};

#[allow(dead_code)]
pub const DEFAULT_FEATURE_NAMES: &[&str] = &[
    "secs_in",
    "secs_left",
    "price_vs_open",
    "ret_15s",
    "ret_30s",
    "ret_60s",
    "ret_180s",
    "rv_15s",
    "rv_30s",
    "rv_60s",
    "rv_180s",
    "volume_15s",
    "volume_30s",
    "volume_60s",
    "volume_180s",
    "imbalance_15s",
    "imbalance_30s",
    "imbalance_60s",
    "imbalance_180s",
    "trade_count",
    "trades_per_sec",
    "combined_prob_up",
    "drift_prob_up",
    "signal_confidence",
    "path_eff",
    "autocorr",
    "ofi_accel",
    "adaptive_confirm",
    "vol_1s",
    "regime_trend",
    "regime_neutral",
    "regime_chop",
    "up_best_bid",
    "up_best_ask",
    "down_best_bid",
    "down_best_ask",
    "up_spread",
    "down_spread",
    "sum_bid",
    "sum_ask",
    "mid_up",
    "mid_down",
    "market_mid_prior_up",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ScorerMode {
    Disabled,
    Shadow,
    Active,
}

impl ScorerMode {
    pub fn parse(value: &str) -> Self {
        match value.to_ascii_lowercase().as_str() {
            "shadow" => Self::Shadow,
            "active" => Self::Active,
            _ => Self::Disabled,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Disabled => "disabled",
            Self::Shadow => "shadow",
            Self::Active => "active",
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ArtifactMetrics {
    pub auc_roc: Option<f64>,
    pub brier: Option<f64>,
    pub ece: Option<f64>,
    pub log_loss: Option<f64>,
    pub positive_ev_hit_rate: Option<f64>,
    pub positive_ev_trades: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BinaryModelArtifact {
    pub artifact_version: String,
    pub generated_at: String,
    pub feature_names: Vec<String>,
    pub means: Vec<f64>,
    pub stds: Vec<f64>,
    pub weights: Vec<f64>,
    pub intercept: f64,
    pub calibration_method: Option<String>,
    pub platt_a: Option<f64>,
    pub platt_b: Option<f64>,
    pub fee_rate: f64,
    pub slippage: f64,
    pub source_export_path: Option<String>,
    #[serde(default)]
    pub metrics: ArtifactMetrics,
}

impl BinaryModelArtifact {
    pub fn load_from_path(path: &str) -> Result<Self> {
        let raw = fs::read_to_string(path)
            .with_context(|| format!("failed to read calibrated artifact at {}", path))?;
        let artifact: Self = serde_json::from_str(&raw)
            .with_context(|| format!("failed to parse calibrated artifact at {}", path))?;
        artifact.validate()?;
        Ok(artifact)
    }

    pub fn validate(&self) -> Result<()> {
        let n = self.feature_names.len();
        if n == 0 {
            return Err(anyhow!("artifact has no feature names"));
        }
        if self.weights.len() != n || self.means.len() != n || self.stds.len() != n {
            return Err(anyhow!(
                "artifact dimensions mismatch: features={} weights={} means={} stds={}",
                n,
                self.weights.len(),
                self.means.len(),
                self.stds.len()
            ));
        }
        Ok(())
    }

    pub fn score_snapshot(&self, snapshot: &CalibratedFeatureSnapshot) -> Result<BinaryModelScore> {
        let ordered = snapshot.ordered_values(&self.feature_names)?;
        let raw_logit = ordered
            .iter()
            .zip(
                self.means
                    .iter()
                    .zip(self.stds.iter())
                    .zip(self.weights.iter()),
            )
            .fold(self.intercept, |acc, (value, ((mean, std), weight))| {
                let denom = if std.abs() <= 1e-12 { 1.0 } else { *std };
                acc + (((*value - *mean) / denom) * *weight)
            });
        let raw_prob_up = sigmoid(raw_logit);
        let calibrated_prob_up = self.calibrate_probability(raw_prob_up);

        Ok(BinaryModelScore {
            raw_logit,
            raw_prob_up,
            calibrated_prob_up,
        })
    }

    pub fn calibrate_probability(&self, raw_prob: f64) -> f64 {
        match (self.platt_a, self.platt_b) {
            (Some(a), Some(b)) => {
                let clipped = raw_prob.clamp(1e-6, 1.0 - 1e-6);
                let logit = (clipped / (1.0 - clipped)).ln();
                sigmoid(a * logit + b)
            }
            _ => raw_prob.clamp(0.0, 1.0),
        }
    }

    pub fn artifact_label(&self) -> String {
        format!("{}@{}", self.artifact_version, self.generated_at)
    }
}

#[derive(Debug, Clone)]
pub struct BinaryModelScore {
    #[allow(dead_code)]
    pub raw_logit: f64,
    pub raw_prob_up: f64,
    pub calibrated_prob_up: f64,
}

#[derive(Debug, Clone)]
pub struct SideSelection {
    pub direction: String,
    pub entry_ask: f64,
    pub entry_bid: f64,
    pub selected_side_prob: f64,
    pub selected_side_edge: f64,
    pub best_ev: f64,
    #[allow(dead_code)]
    pub ev_up: f64,
    #[allow(dead_code)]
    pub ev_down: f64,
    #[allow(dead_code)]
    pub market_mid_prior_up: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CalibratedFeatureSnapshot {
    pub secs_in: f64,
    pub secs_left: f64,
    pub price_vs_open: f64,
    pub ret_15s: f64,
    pub ret_30s: f64,
    pub ret_60s: f64,
    pub ret_180s: f64,
    pub rv_15s: f64,
    pub rv_30s: f64,
    pub rv_60s: f64,
    pub rv_180s: f64,
    pub volume_15s: f64,
    pub volume_30s: f64,
    pub volume_60s: f64,
    pub volume_180s: f64,
    pub imbalance_15s: f64,
    pub imbalance_30s: f64,
    pub imbalance_60s: f64,
    pub imbalance_180s: f64,
    pub trade_count: f64,
    pub trades_per_sec: f64,
    pub combined_prob_up: f64,
    pub drift_prob_up: f64,
    pub signal_confidence: f64,
    pub path_eff: f64,
    pub autocorr: f64,
    pub ofi_accel: f64,
    pub adaptive_confirm: f64,
    pub vol_1s: f64,
    pub regime_trend: f64,
    pub regime_neutral: f64,
    pub regime_chop: f64,
    pub up_best_bid: f64,
    pub up_best_ask: f64,
    pub down_best_bid: f64,
    pub down_best_ask: f64,
    pub up_spread: f64,
    pub down_spread: f64,
    pub sum_bid: f64,
    pub sum_ask: f64,
    pub mid_up: f64,
    pub mid_down: f64,
    pub market_mid_prior_up: f64,
}

impl CalibratedFeatureSnapshot {
    pub fn ordered_values(&self, feature_names: &[String]) -> Result<Vec<f64>> {
        feature_names
            .iter()
            .map(|name| {
                self.value_for(name)
                    .ok_or_else(|| anyhow!("feature '{}' not available in runtime snapshot", name))
            })
            .collect()
    }

    fn value_for(&self, feature_name: &str) -> Option<f64> {
        match feature_name {
            "secs_in" => Some(self.secs_in),
            "secs_left" => Some(self.secs_left),
            "price_vs_open" => Some(self.price_vs_open),
            "ret_15s" => Some(self.ret_15s),
            "ret_30s" => Some(self.ret_30s),
            "ret_60s" => Some(self.ret_60s),
            "ret_180s" => Some(self.ret_180s),
            "rv_15s" => Some(self.rv_15s),
            "rv_30s" => Some(self.rv_30s),
            "rv_60s" => Some(self.rv_60s),
            "rv_180s" => Some(self.rv_180s),
            "volume_15s" => Some(self.volume_15s),
            "volume_30s" => Some(self.volume_30s),
            "volume_60s" => Some(self.volume_60s),
            "volume_180s" => Some(self.volume_180s),
            "imbalance_15s" => Some(self.imbalance_15s),
            "imbalance_30s" => Some(self.imbalance_30s),
            "imbalance_60s" => Some(self.imbalance_60s),
            "imbalance_180s" => Some(self.imbalance_180s),
            "trade_count" => Some(self.trade_count),
            "trades_per_sec" => Some(self.trades_per_sec),
            "combined_prob_up" => Some(self.combined_prob_up),
            "drift_prob_up" => Some(self.drift_prob_up),
            "signal_confidence" => Some(self.signal_confidence),
            "path_eff" => Some(self.path_eff),
            "autocorr" => Some(self.autocorr),
            "ofi_accel" => Some(self.ofi_accel),
            "adaptive_confirm" => Some(self.adaptive_confirm),
            "vol_1s" => Some(self.vol_1s),
            "regime_trend" => Some(self.regime_trend),
            "regime_neutral" => Some(self.regime_neutral),
            "regime_chop" => Some(self.regime_chop),
            "up_best_bid" => Some(self.up_best_bid),
            "up_best_ask" => Some(self.up_best_ask),
            "down_best_bid" => Some(self.down_best_bid),
            "down_best_ask" => Some(self.down_best_ask),
            "up_spread" => Some(self.up_spread),
            "down_spread" => Some(self.down_spread),
            "sum_bid" => Some(self.sum_bid),
            "sum_ask" => Some(self.sum_ask),
            "mid_up" => Some(self.mid_up),
            "mid_down" => Some(self.mid_down),
            "market_mid_prior_up" => Some(self.market_mid_prior_up),
            _ => None,
        }
    }
}

pub fn build_raw_1s_arrays(
    trades: &[BinanceTrade],
    start_ms: i64,
    duration_secs: u64,
) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let n_secs = duration_secs as usize;
    let mut close = vec![0.0_f64; n_secs];
    let mut buy_vol = vec![0.0_f64; n_secs];
    let mut sell_vol = vec![0.0_f64; n_secs];

    for trade in trades {
        let delta_ms = trade.trade_time_ms.saturating_sub(start_ms);
        let sec_idx = (delta_ms / 1000) as usize;
        if sec_idx >= n_secs {
            continue;
        }
        close[sec_idx] = trade.price;
        if trade.is_buyer_maker {
            sell_vol[sec_idx] += trade.quantity;
        } else {
            buy_vol[sec_idx] += trade.quantity;
        }
    }

    (close, buy_vol, sell_vol)
}

pub fn build_1s_bars_from_arrays(
    raw_close: &[f64],
    raw_buy_vol: &[f64],
    raw_sell_vol: &[f64],
    secs_in: u64,
) -> Option<OneSecondBars> {
    let n_secs = (secs_in + 1) as usize;
    if raw_close.len() < n_secs || raw_buy_vol.len() < n_secs || raw_sell_vol.len() < n_secs {
        return None;
    }

    let mut close = raw_close[..n_secs].to_vec();
    let buy_vol = raw_buy_vol[..n_secs].to_vec();
    let sell_vol = raw_sell_vol[..n_secs].to_vec();

    let mut last_valid = 0.0;
    for value in &mut close {
        if *value > 0.0 {
            last_valid = *value;
        } else {
            *value = last_valid;
        }
    }

    if let Some(first_valid) = close.iter().copied().find(|price| *price > 0.0) {
        for value in &mut close {
            if *value == 0.0 {
                *value = first_valid;
            } else {
                break;
            }
        }
    } else {
        return None;
    }

    Some(OneSecondBars {
        close,
        buy_vol,
        sell_vol,
    })
}

pub fn build_1s_bars(trades: &[BinanceTrade], start_ms: i64, secs_in: u64) -> OneSecondBars {
    let (raw_close, raw_buy_vol, raw_sell_vol) = build_raw_1s_arrays(trades, start_ms, secs_in + 1);

    build_1s_bars_from_arrays(&raw_close, &raw_buy_vol, &raw_sell_vol, secs_in).unwrap_or(
        OneSecondBars {
            close: vec![0.0; (secs_in + 1) as usize],
            buy_vol: vec![0.0; (secs_in + 1) as usize],
            sell_vol: vec![0.0; (secs_in + 1) as usize],
        },
    )
}

pub fn build_calibrated_feature_snapshot(
    bars: &OneSecondBars,
    open_price: f64,
    market: &MarketInfo,
    secs_in: u64,
    drift: &DriftSignal,
    trade_count: usize,
) -> CalibratedFeatureSnapshot {
    let current_price = bars.close.last().copied().unwrap_or(open_price).max(1e-9);
    let secs_left = 900_u64.saturating_sub(secs_in);

    let mid_up = mid(market.up_best_bid, market.up_best_ask);
    let mid_down = mid(market.down_best_bid, market.down_best_ask);
    let sum_mid = mid_up + mid_down;
    let market_mid_prior_up = if sum_mid > 0.0 {
        (mid_up / sum_mid).clamp(0.0, 1.0)
    } else {
        0.5
    };

    CalibratedFeatureSnapshot {
        secs_in: secs_in as f64,
        secs_left: secs_left as f64,
        price_vs_open: if open_price > 0.0 {
            (current_price / open_price) - 1.0
        } else {
            0.0
        },
        ret_15s: pct_ret(&bars.close, 15),
        ret_30s: pct_ret(&bars.close, 30),
        ret_60s: pct_ret(&bars.close, 60),
        ret_180s: pct_ret(&bars.close, 180),
        rv_15s: realized_vol(&bars.close, 15),
        rv_30s: realized_vol(&bars.close, 30),
        rv_60s: realized_vol(&bars.close, 60),
        rv_180s: realized_vol(&bars.close, 180),
        volume_15s: window_sum_total(&bars.buy_vol, &bars.sell_vol, 15),
        volume_30s: window_sum_total(&bars.buy_vol, &bars.sell_vol, 30),
        volume_60s: window_sum_total(&bars.buy_vol, &bars.sell_vol, 60),
        volume_180s: window_sum_total(&bars.buy_vol, &bars.sell_vol, 180),
        imbalance_15s: window_imbalance(&bars.buy_vol, &bars.sell_vol, 15),
        imbalance_30s: window_imbalance(&bars.buy_vol, &bars.sell_vol, 30),
        imbalance_60s: window_imbalance(&bars.buy_vol, &bars.sell_vol, 60),
        imbalance_180s: window_imbalance(&bars.buy_vol, &bars.sell_vol, 180),
        trade_count: trade_count as f64,
        trades_per_sec: trade_count as f64 / (secs_in.max(1) as f64),
        combined_prob_up: drift.combined_prob_up,
        drift_prob_up: drift.drift_prob_up,
        signal_confidence: drift.confidence,
        path_eff: drift.path_eff,
        autocorr: drift.autocorr,
        ofi_accel: drift.ofi_accel,
        adaptive_confirm: drift.adaptive_confirm as f64,
        vol_1s: drift.vol_1s,
        regime_trend: if matches!(drift.regime, Regime::Trend) {
            1.0
        } else {
            0.0
        },
        regime_neutral: if matches!(drift.regime, Regime::Neutral) {
            1.0
        } else {
            0.0
        },
        regime_chop: if matches!(drift.regime, Regime::Chop) {
            1.0
        } else {
            0.0
        },
        up_best_bid: market.up_best_bid.max(0.0),
        up_best_ask: market.up_best_ask.max(0.0),
        down_best_bid: market.down_best_bid.max(0.0),
        down_best_ask: market.down_best_ask.max(0.0),
        up_spread: spread(market.up_best_bid, market.up_best_ask),
        down_spread: spread(market.down_best_bid, market.down_best_ask),
        sum_bid: (market.up_best_bid + market.down_best_bid).max(0.0),
        sum_ask: (market.up_best_ask + market.down_best_ask).max(0.0),
        mid_up,
        mid_down,
        market_mid_prior_up,
    }
}

pub fn compute_expected_value(
    prob_win: f64,
    ask: f64,
    fee_rate: f64,
    slippage: f64,
) -> Option<f64> {
    if !prob_win.is_finite() || !ask.is_finite() || ask <= 0.0 {
        return None;
    }

    let entry_price = ask + slippage;
    if entry_price <= 0.0 {
        return None;
    }

    Some(((prob_win.clamp(0.0, 1.0) * (1.0 - fee_rate)) / entry_price) - (1.0 + fee_rate))
}

pub fn select_best_side(
    prob_up: f64,
    market: &MarketInfo,
    fee_rate: f64,
    slippage: f64,
    min_ev: f64,
) -> Option<SideSelection> {
    let ev_up = compute_expected_value(prob_up, market.up_best_ask, fee_rate, slippage)?;
    let ev_down = compute_expected_value(1.0 - prob_up, market.down_best_ask, fee_rate, slippage)?;

    if ev_up <= min_ev && ev_down <= min_ev {
        return None;
    }

    let mid_up = mid(market.up_best_bid, market.up_best_ask);
    let mid_down = mid(market.down_best_bid, market.down_best_ask);
    let market_mid_prior_up = if (mid_up + mid_down) > 0.0 {
        (mid_up / (mid_up + mid_down)).clamp(0.0, 1.0)
    } else {
        0.5
    };

    if ev_up >= ev_down {
        Some(SideSelection {
            direction: "UP".to_string(),
            entry_ask: market.up_best_ask,
            entry_bid: market.up_best_bid,
            selected_side_prob: prob_up.clamp(0.0, 1.0),
            selected_side_edge: prob_up - market.up_best_ask - slippage,
            best_ev: ev_up,
            ev_up,
            ev_down,
            market_mid_prior_up,
        })
    } else {
        Some(SideSelection {
            direction: "DOWN".to_string(),
            entry_ask: market.down_best_ask,
            entry_bid: market.down_best_bid,
            selected_side_prob: (1.0 - prob_up).clamp(0.0, 1.0),
            selected_side_edge: (1.0 - prob_up) - market.down_best_ask - slippage,
            best_ev: ev_down,
            ev_up,
            ev_down,
            market_mid_prior_up,
        })
    }
}

fn window_slice(series: &[f64], window: usize) -> &[f64] {
    if series.len() <= window {
        series
    } else {
        &series[series.len() - window..]
    }
}

fn pct_ret(series: &[f64], window: usize) -> f64 {
    if series.len() <= window {
        return 0.0;
    }

    let end = series[series.len() - 1];
    let start = series[series.len() - 1 - window];
    if start <= 0.0 {
        0.0
    } else {
        (end / start) - 1.0
    }
}

fn realized_vol(close: &[f64], window: usize) -> f64 {
    let slice = window_slice(close, window + 1);
    if slice.len() < 3 {
        return 0.0;
    }

    let returns: Vec<f64> = slice
        .windows(2)
        .map(|window| ((window[1] + 1e-9) / (window[0] + 1e-9)).ln())
        .collect();
    let mean = returns.iter().sum::<f64>() / returns.len() as f64;
    let var = returns.iter().map(|ret| (ret - mean).powi(2)).sum::<f64>() / returns.len() as f64;
    var.sqrt()
}

fn window_sum_total(buy: &[f64], sell: &[f64], window: usize) -> f64 {
    let buy_slice = window_slice(buy, window);
    let sell_slice = window_slice(sell, window);
    buy_slice.iter().sum::<f64>() + sell_slice.iter().sum::<f64>()
}

fn window_imbalance(buy: &[f64], sell: &[f64], window: usize) -> f64 {
    let buy_sum = window_slice(buy, window).iter().sum::<f64>();
    let sell_sum = window_slice(sell, window).iter().sum::<f64>();
    let denom = buy_sum + sell_sum;
    if denom <= 1e-12 {
        0.0
    } else {
        (buy_sum - sell_sum) / denom
    }
}

fn spread(bid: f64, ask: f64) -> f64 {
    if bid > 0.0 && ask > 0.0 {
        (ask - bid).max(0.0)
    } else {
        0.0
    }
}

fn mid(bid: f64, ask: f64) -> f64 {
    if bid > 0.0 && ask > 0.0 {
        (bid + ask) / 2.0
    } else {
        0.0
    }
}

fn sigmoid(x: f64) -> f64 {
    if x >= 0.0 {
        let z = (-x).exp();
        1.0 / (1.0 + z)
    } else {
        let z = x.exp();
        z / (1.0 + z)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_market() -> MarketInfo {
        MarketInfo {
            slug: "btc-updown-15m-1775410000".to_string(),
            start_ms: 1,
            end_ms: 900_001,
            up_price: 0.54,
            down_price: 0.46,
            up_best_ask: 0.55,
            down_best_ask: 0.47,
            up_best_bid: 0.53,
            down_best_bid: 0.45,
        }
    }

    #[test]
    fn expected_value_prefers_positive_side() {
        let selection = select_best_side(0.66, &sample_market(), 0.01, 0.005, 0.0).unwrap();
        assert_eq!(selection.direction, "UP");
        assert!(selection.ev_up > selection.ev_down);
    }

    #[test]
    fn raw_arrays_builds_non_empty_bars() {
        let trades = vec![
            BinanceTrade {
                trade_time_ms: 1_000,
                price: 100_000.0,
                quantity: 0.1,
                is_buyer_maker: false,
            },
            BinanceTrade {
                trade_time_ms: 2_000,
                price: 100_010.0,
                quantity: 0.2,
                is_buyer_maker: true,
            },
        ];
        let (close, buy, sell) = build_raw_1s_arrays(&trades, 0, 10);
        let bars = build_1s_bars_from_arrays(&close, &buy, &sell, 3).unwrap();
        assert_eq!(bars.close.len(), 4);
        assert!(bars.close[1] > 0.0);
    }
}
