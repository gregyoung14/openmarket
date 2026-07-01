//! Signal scanner — runs the v9.2 regime-aware drift estimator on a 1-second
//! interval during active markets. Manages regime gating, hour blacklist,
//! and adaptive confirmation window.

use btc_common::version;
use crate::config;
use crate::drift::compute_drift_signal_v11;
use crate::models::{BinanceTrade, OneSecondBars, Regime, SignalMessage};
use crate::state::{AppState, BestEntryCandidate};
use crate::volume::hourly_volume_rate;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tracing::{debug, info};

/// Build 1-second OHLC bars from raw trade buffer.
///
/// Aggregates trades into per-second close prices, buy volume, and sell volume.
/// Forward-fills gaps (seconds with no trades) and backward-fills leading zeros.
fn build_1s_bars(trades: &[BinanceTrade], start_ms: i64, secs_in: u64) -> OneSecondBars {
    let n_secs = (secs_in + 1) as usize;
    let mut close = vec![0.0_f64; n_secs];
    let mut buy_vol = vec![0.0_f64; n_secs];
    let mut sell_vol = vec![0.0_f64; n_secs];

    for trade in trades {
        let sec_idx = ((trade.trade_time_ms - start_ms) / 1000) as usize;
        let sec_idx = sec_idx.min(n_secs.saturating_sub(1));

        close[sec_idx] = trade.price; // last trade in each second = close
        if trade.is_buyer_maker {
            sell_vol[sec_idx] += trade.quantity;
        } else {
            buy_vol[sec_idx] += trade.quantity;
        }
    }

    // Forward-fill close prices (gaps where no trade occurred)
    let mut last_valid = 0.0;
    for c in close.iter_mut() {
        if *c > 0.0 {
            last_valid = *c;
        } else {
            *c = last_valid;
        }
    }
    // Backward-fill any leading zeros
    if let Some(&first_valid) = close.iter().find(|&&c| c > 0.0) {
        for c in close.iter_mut() {
            if *c == 0.0 {
                *c = first_valid;
            } else {
                break;
            }
        }
    }

    OneSecondBars {
        close,
        buy_vol,
        sell_vol,
    }
}

/// Spawns the signal scanner task that runs every second during active markets.
pub fn spawn_signal_scanner(state: AppState) {
    tokio::spawn(signal_scan_loop(state));
}

fn emit_best_candidate(state: &AppState, market: &crate::models::MarketInfo, now_ms: i64) {
    if *state.entry_fired.lock() {
        return;
    }

    if let Some(candidate) = state.take_best_candidate() {
        *state.entry_fired.lock() = true;
        state.update_stats(|s| {
            s.signals_confirmed += 1;
            s.entries_fired += 1;
        });

        info!(
            "🚀 v11 ENTRY SIGNAL (best-candidate): {} on {} conf={:.3} cons={:.2} ask={:.4} edge={:.4} regime={} confirm={}s @ {}s",
            candidate.direction,
            market.slug,
            candidate.confidence,
            candidate.consistency,
            candidate.entry_ask,
            candidate.edge,
            candidate.regime,
            candidate.adaptive_confirm,
            candidate.secs_in,
        );

        let entry_msg = SignalMessage {
            msg_type: "entry".to_string(),
            direction: Some(candidate.direction),
            confidence: Some(candidate.confidence),
            consistency: Some(candidate.consistency),
            raw_prob: Some(candidate.combined_prob_up),
            combined_prob_up: Some(candidate.combined_prob_up),
            drift_prob_up: Some(candidate.drift_prob_up),
            market: Some(market.slug.clone()),
            secs_in: Some(candidate.secs_in),
            secs_left: Some(config::MARKET_DURATION_SECS.saturating_sub(candidate.secs_in)),
            entry_ask: Some(candidate.entry_ask),
            entry_bid: Some(candidate.entry_bid),
            btc_price: state.get_stats().last_btc_price,
            n_trades: Some(candidate.n_trades),
            edge: Some(candidate.edge),
            regime: Some(candidate.regime),
            path_eff: Some(candidate.path_eff),
            autocorr: Some(candidate.autocorr),
            ofi_accel: Some(candidate.ofi_accel),
            adaptive_confirm: Some(candidate.adaptive_confirm),
            vol_1s: Some(candidate.vol_1s),
            timestamp: now_ms,
            version: version::SIGNAL_VERSION.to_string(),
        };

        if let Ok(json) = serde_json::to_string(&entry_msg) {
            let _ = state.signal_tx.send(json);
        }
    } else {
        *state.entry_fired.lock() = true;
        info!(
            "⏭️ v11: no qualified candidate found for {} in entry window",
            market.slug
        );
    }
}

async fn signal_scan_loop(state: AppState) {
    let interval = Duration::from_millis(config::SIGNAL_SCAN_INTERVAL_MS);

    loop {
        tokio::time::sleep(interval).await;

        // Only scan if we have a market and haven't already fired
        let market = match state.get_market() {
            Some(m) => m,
            None => continue,
        };

        if *state.entry_fired.lock() {
            continue;
        }

        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;

        let market_epoch_s = market.start_ms / 1000;
        let (_, market_hour_et) = config::et_day_hour(market_epoch_s);
        if config::is_blacklisted_epoch(market_epoch_s) {
            debug!(
                "⛔ v11 BLACKLIST: {} starts at {:02}h ET — skipping market",
                market.slug, market_hour_et
            );
            continue;
        }

        let secs_in = ((now_ms - market.start_ms) / 1000) as u64;

        // Check timing window
        if secs_in < config::MIN_SECS_INTO_MARKET {
            continue;
        }
        if secs_in > config::MAX_SECS_INTO_MARKET {
            emit_best_candidate(&state, &market, now_ms);
            continue;
        }

        let remaining_secs = config::MARKET_DURATION_SECS.saturating_sub(secs_in);

        // Get trade buffer and open price
        let open_price = match state.get_open_price() {
            Some(p) => p,
            None => continue,
        };

        let trades = state.get_trades_snapshot();

        // Need minimum trades before we can compute anything
        if trades.len() < config::MIN_TRADES_FOR_SIGNAL {
            continue;
        }

        // ── Build 1-second bars from trade buffer ──
        let bars = build_1s_bars(&trades, market.start_ms, secs_in);

        // ── Compute v11 drift signal ──
        let signal = match compute_drift_signal_v11(&bars, open_price, remaining_secs as f64) {
            Some(s) => s,
            None => continue,
        };

        // ── REGIME GATE (v9) ──
        // If regime is chop, reset confirmation and skip this tick entirely
        if signal.regime == Regime::Chop {
            state.confirmation.lock().reset();
            state.update_stats(|s| {
                s.signals_computed += 1;
                s.last_signal_direction = Some(signal.direction.clone());
                s.last_signal_confidence = Some(signal.confidence);
                s.last_signal_time = Some(now_ms);
                s.last_regime = Some("chop".to_string());
                s.last_path_eff = Some(signal.path_eff);
                s.last_adaptive_confirm = Some(signal.adaptive_confirm);
            });

            let n = state.get_stats().signals_computed;
            if n % 30 == 0 {
                info!(
                    "🌀 Signal #{}: CHOP regime — skipped (path_eff={:.3} autocorr={:.3}) @ {}s",
                    n, signal.path_eff, signal.autocorr, secs_in,
                );
            }
            continue;
        }

        state.update_stats(|s| {
            s.signals_computed += 1;
            s.last_signal_direction = Some(signal.direction.clone());
            s.last_signal_confidence = Some(signal.confidence);
            s.last_signal_time = Some(now_ms);
            s.last_regime = Some(signal.regime.to_string());
            s.last_path_eff = Some(signal.path_eff);
            s.last_adaptive_confirm = Some(signal.adaptive_confirm);
        });

        // Log every 30th signal to avoid spam
        let n = state.get_stats().signals_computed;
        if n % 30 == 0 {
            info!(
                "🔮 Signal #{}: {} conf={:.3} drift_p={:.3} ofi_a={:.3} sb={:.6} cons={:.2} regime={} path_eff={:.3} confirm={}s @ {}s",
                n,
                signal.direction,
                signal.confidence,
                signal.drift_prob_up,
                signal.ofi_accel,
                signal.scoreboard,
                signal.consistency,
                signal.regime,
                signal.path_eff,
                signal.adaptive_confirm,
                secs_in,
            );
        }

        // Broadcast prediction to downstream
        let pred_msg = SignalMessage {
            msg_type: "prediction".to_string(),
            direction: Some(signal.direction.clone()),
            confidence: Some(signal.confidence),
            consistency: Some(signal.consistency),
            raw_prob: Some(signal.combined_prob_up),
            combined_prob_up: Some(signal.combined_prob_up),
            drift_prob_up: Some(signal.drift_prob_up),
            market: Some(market.slug.clone()),
            secs_in: Some(secs_in),
            secs_left: Some(remaining_secs),
            entry_ask: None,
            entry_bid: None,
            btc_price: state.get_stats().last_btc_price,
            n_trades: Some(trades.len()),
            edge: None,
            regime: Some(signal.regime.to_string()),
            path_eff: Some(signal.path_eff),
            autocorr: Some(signal.autocorr),
            ofi_accel: Some(signal.ofi_accel),
            adaptive_confirm: Some(signal.adaptive_confirm),
            vol_1s: Some(signal.vol_1s),
            timestamp: now_ms,
            version: version::SIGNAL_VERSION.to_string(),
        };
        if let Ok(json) = serde_json::to_string(&pred_msg) {
            let _ = state.signal_tx.send(json);
        }

        // ── Adaptive Confirmation window (v9) ──
        let confirmed = {
            let mut conf = state.confirmation.lock();
            let result = conf.update(
                &signal.direction,
                signal.confidence,
                config::ENTRY_CONFIDENCE,
                secs_in,
                signal.adaptive_confirm, // v9.2: dynamic window from signal
            );
            state.update_stats(|s| {
                s.confirmation_count = conf.count;
                s.confirmation_direction = conf.direction.clone();
            });
            result
        };

        if confirmed {
            // ── v11 ENTRY FILTERS ──
            let (entry_ask, entry_bid) = if signal.direction == "UP" {
                (market.up_best_ask, market.up_best_bid)
            } else {
                (market.down_best_ask, market.down_best_bid)
            };

            // Price floor — skip penny contracts where market has priced outcome
            if entry_ask < config::MIN_ENTRY_PRICE {
                info!(
                    "⛔ v11 SKIP (penny contract): {} ask={:.4} < MIN {:.2} on {} @ {}s",
                    signal.direction, entry_ask, config::MIN_ENTRY_PRICE,
                    market.slug, secs_in,
                );
                state.confirmation.lock().reset();
                continue;
            }

            // Price cap — skip expensive contracts
            if entry_ask > config::MAX_ENTRY_PRICE {
                info!(
                    "⛔ v9.2 SKIP (price cap): {} ask={:.4} > MAX {:.2} on {} @ {}s",
                    signal.direction, entry_ask, config::MAX_ENTRY_PRICE,
                    market.slug, secs_in,
                );
                state.confirmation.lock().reset();
                continue;
            }

            // EV edge filter — confidence must exceed entry price + slippage
            let entry_price_with_slippage = entry_ask + config::SLIPPAGE;
            let edge = signal.confidence - entry_price_with_slippage;
            if edge < config::MIN_EDGE {
                info!(
                    "⛔ v11 SKIP (low edge): {} edge={:.4} < {:.2} (conf={:.3}, price={:.4}) on {} @ {}s",
                    signal.direction, edge, config::MIN_EDGE,
                    signal.confidence, entry_price_with_slippage,
                    market.slug, secs_in,
                );
                state.confirmation.lock().reset();
                continue;
            }

            if config::ENABLE_VOLUME_GATE {
                if let Some(median_hourly_volume) = state.volume_median() {
                    let window_volume: f64 = trades.iter().map(|t| t.quantity).sum();
                    let observed_duration = (secs_in.max(1)) as f64;
                    let observed_hourly_rate = hourly_volume_rate(window_volume, observed_duration);
                    if observed_hourly_rate < median_hourly_volume {
                        info!(
                            "⛔ v11 SKIP (volume gate): hourly_rate={:.2} < median={:.2} on {} @ {}s",
                            observed_hourly_rate,
                            median_hourly_volume,
                            market.slug,
                            secs_in,
                        );
                        state.confirmation.lock().reset();
                        continue;
                    }
                }
            }

            let candidate = BestEntryCandidate {
                direction: signal.direction,
                confidence: signal.confidence,
                consistency: signal.consistency,
                combined_prob_up: signal.combined_prob_up,
                drift_prob_up: signal.drift_prob_up,
                regime: signal.regime.to_string(),
                path_eff: signal.path_eff,
                autocorr: signal.autocorr,
                ofi_accel: signal.ofi_accel,
                adaptive_confirm: signal.adaptive_confirm,
                vol_1s: signal.vol_1s,
                edge,
                entry_ask,
                entry_bid,
                secs_in,
                n_trades: trades.len(),
            };

            if state.consider_best_candidate(candidate.clone()) {
                info!(
                    "⭐ v11 best candidate updated: {} conf={:.3} edge={:.4} ask={:.4} @ {}s",
                    candidate.direction,
                    candidate.confidence,
                    candidate.edge,
                    candidate.entry_ask,
                    candidate.secs_in,
                );
            }
        }
    }
}
