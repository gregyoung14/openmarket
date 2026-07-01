//! Signal scanner — runs the v9.2 regime-aware drift estimator on a 1-second
//! interval during active markets. Manages regime gating, hour blacklist,
//! and adaptive confirmation window.

use crate::calibrated::{
    build_1s_bars, build_calibrated_feature_snapshot, compute_expected_value, select_best_side,
    ScorerMode,
};
use crate::config;
use crate::drift::compute_drift_signal_v14;
use crate::models::{OneSecondBars, Regime, SignalMessage};
use crate::state::{AppState, BestEntryCandidate};
use crate::volume::hourly_volume_rate;
use btc_common::version;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tracing::{debug, info};

#[derive(Clone)]
struct CalibratedContext {
    raw_model_prob_up: f64,
    calibrated_prob_up: f64,
    ev_up: Option<f64>,
    ev_down: Option<f64>,
    selected: Option<crate::calibrated::SideSelection>,
    artifact_version: String,
}

fn maybe_score_calibrated(
    state: &AppState,
    bars: &OneSecondBars,
    open_price: f64,
    market: &crate::models::MarketInfo,
    secs_in: u64,
    signal: &crate::models::DriftSignal,
    trades_len: usize,
) -> Option<CalibratedContext> {
    if state.scorer_mode == ScorerMode::Disabled
        || secs_in % config::calibrated_score_interval_secs() != 0
    {
        return None;
    }

    let model = state.calibrated_model.as_ref()?;
    let snapshot =
        build_calibrated_feature_snapshot(bars, open_price, market, secs_in, signal, trades_len);
    let score = model.score_snapshot(&snapshot).ok()?;
    let ev_up = compute_expected_value(
        score.calibrated_prob_up,
        market.up_best_ask,
        model.fee_rate,
        model.slippage,
    );
    let ev_down = compute_expected_value(
        1.0 - score.calibrated_prob_up,
        market.down_best_ask,
        model.fee_rate,
        model.slippage,
    );
    let selected = select_best_side(
        score.calibrated_prob_up,
        market,
        model.fee_rate,
        model.slippage,
        config::calibrated_min_ev(),
    );

    state.update_stats(|stats| {
        stats.calibrated_scores_computed += 1;
        stats.last_calibrated_prob_up = Some(score.calibrated_prob_up);
        stats.last_calibrated_ev_up = ev_up;
        stats.last_calibrated_ev_down = ev_down;
        stats.last_calibrated_direction = selected
            .as_ref()
            .map(|selection| selection.direction.clone());
    });

    Some(CalibratedContext {
        raw_model_prob_up: score.raw_prob_up,
        calibrated_prob_up: score.calibrated_prob_up,
        ev_up,
        ev_down,
        selected,
        artifact_version: model.artifact_label(),
    })
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
            "🚀 {} ENTRY SIGNAL (best-candidate): {} on {} conf={:.3} cons={:.2} ask={:.4} edge={:.4} rank={}({:.4}) regime={} confirm={}s @ {}s",
            version::SIGNAL_VERSION,
            candidate.direction,
            market.slug,
            candidate.confidence,
            candidate.consistency,
            candidate.entry_ask,
            candidate.edge,
            candidate.ranking_basis,
            candidate.ranking_score,
            candidate.regime,
            candidate.adaptive_confirm,
            candidate.secs_in,
        );

        let entry_msg = SignalMessage {
            msg_type: "entry".to_string(),
            direction: Some(candidate.direction),
            confidence: Some(candidate.confidence),
            consistency: Some(candidate.consistency),
            raw_prob: candidate
                .calibrated_prob_up
                .or(Some(candidate.combined_prob_up)),
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
            scoring_mode: Some(candidate.scoring_mode),
            ranking_basis: Some(candidate.ranking_basis),
            ranking_score: Some(candidate.ranking_score),
            raw_model_prob_up: candidate.raw_model_prob_up,
            calibrated_prob_up: candidate.calibrated_prob_up,
            selected_side_prob: candidate.selected_side_prob,
            ev_up: candidate.ev_up,
            ev_down: candidate.ev_down,
            artifact_version: candidate.artifact_version,
        };

        if let Ok(json) = serde_json::to_string(&entry_msg) {
            let _ = state.signal_tx.send(json);
        }
    } else {
        *state.entry_fired.lock() = true;
        info!(
            "⏭️ {}: no qualified candidate found for {} in entry window",
            version::SIGNAL_VERSION,
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
                "⛔ {} BLACKLIST: {} starts at {:02}h ET — skipping market",
                version::SIGNAL_VERSION,
                market.slug,
                market_hour_et
            );
            continue;
        }

        let secs_in = ((now_ms - market.start_ms) / 1000) as u64;

        // Check timing window
        if secs_in < config::min_secs_into_market() {
            continue;
        }
        if secs_in > config::max_secs_into_market() {
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

        // ── Compute v14 drift signal ──
        let signal = match compute_drift_signal_v14(&bars, open_price, remaining_secs as f64) {
            Some(s) => s,
            None => continue,
        };

        // ── REGIME GATE (v9) ──
        // If regime is chop, reset confirmation and skip this tick entirely
        if signal.regime == Regime::Chop {
            state.confirmation.lock().reset();
            state.update_stats(|s| {
                s.signals_computed += 1;
                s.skip_chop_regime += 1;
                s.last_signal_direction = Some(signal.direction.clone());
                s.last_signal_confidence = Some(signal.confidence);
                s.last_signal_time = Some(now_ms);
                s.last_regime = Some("chop".to_string());
                s.last_path_eff = Some(signal.path_eff);
                s.last_adaptive_confirm = Some(signal.adaptive_confirm);
            });

            let n = state.get_stats().signals_computed;
            if n.is_multiple_of(30) {
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
        if n.is_multiple_of(30) {
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

        let calibrated_ctx = maybe_score_calibrated(
            &state,
            &bars,
            open_price,
            &market,
            secs_in,
            &signal,
            trades.len(),
        );

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
            scoring_mode: Some(state.scorer_mode.as_str().to_string()),
            ranking_basis: calibrated_ctx.as_ref().map(|_| {
                if state.scorer_mode == ScorerMode::Active {
                    "ev".to_string()
                } else {
                    "edge".to_string()
                }
            }),
            ranking_score: calibrated_ctx
                .as_ref()
                .and_then(|ctx| ctx.selected.as_ref().map(|selection| selection.best_ev)),
            raw_model_prob_up: calibrated_ctx.as_ref().map(|ctx| ctx.raw_model_prob_up),
            calibrated_prob_up: calibrated_ctx.as_ref().map(|ctx| ctx.calibrated_prob_up),
            selected_side_prob: calibrated_ctx.as_ref().and_then(|ctx| {
                ctx.selected
                    .as_ref()
                    .map(|selection| selection.selected_side_prob)
            }),
            ev_up: calibrated_ctx.as_ref().and_then(|ctx| ctx.ev_up),
            ev_down: calibrated_ctx.as_ref().and_then(|ctx| ctx.ev_down),
            artifact_version: calibrated_ctx
                .as_ref()
                .map(|ctx| ctx.artifact_version.clone()),
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
                config::entry_confidence(),
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
            if state.scorer_mode == ScorerMode::Active
                && secs_in % config::calibrated_score_interval_secs() != 0
            {
                continue;
            }

            // ── v14 ENTRY FILTERS / calibrated side selection ──
            let (mut entry_ask, mut entry_bid) = if signal.direction == "UP" {
                (market.up_best_ask, market.up_best_bid)
            } else {
                (market.down_best_ask, market.down_best_bid)
            };
            let mut direction = signal.direction.clone();
            let mut candidate_confidence = signal.confidence;
            let mut edge = signal.confidence - (entry_ask + config::SLIPPAGE);
            let mut ranking_score = edge;
            let mut ranking_basis = "edge".to_string();

            if state.scorer_mode == ScorerMode::Active {
                let Some(ctx) = calibrated_ctx.as_ref() else {
                    continue;
                };

                let Some(selection) = ctx.selected.as_ref() else {
                    state.update_stats(|stats| stats.skip_calibrated_no_ev += 1);
                    info!(
                        "⛔ {} SKIP (no positive calibrated EV): prob_up={:.3} ev_up={:?} ev_down={:?} on {} @ {}s",
                        version::SIGNAL_VERSION,
                        ctx.calibrated_prob_up,
                        ctx.ev_up,
                        ctx.ev_down,
                        market.slug,
                        secs_in,
                    );
                    continue;
                };

                direction = selection.direction.clone();
                entry_ask = selection.entry_ask;
                entry_bid = selection.entry_bid;
                candidate_confidence = selection.selected_side_prob;
                edge = selection.selected_side_edge;
                ranking_score = selection.best_ev;
                ranking_basis = "ev".to_string();

                state.update_stats(|stats| {
                    stats.calibrated_scores_used += 1;
                    stats.last_ranking_basis = Some("ev".to_string());
                });
            }

            // Price floor — skip penny contracts where market has priced outcome
            if entry_ask < config::min_entry_price() {
                state.update_stats(|s| s.skip_penny_contract += 1);
                info!(
                    "⛔ {} SKIP (penny contract): {} ask={:.4} < MIN {:.2} on {} @ {}s",
                    version::SIGNAL_VERSION,
                    direction,
                    entry_ask,
                    config::min_entry_price(),
                    market.slug,
                    secs_in,
                );
                continue;
            }

            // Price cap — skip expensive contracts
            if entry_ask > config::max_entry_price() {
                state.update_stats(|s| s.skip_price_cap += 1);
                info!(
                    "⛔ {} SKIP (price cap): {} ask={:.4} > MAX {:.2} on {} @ {}s",
                    version::SIGNAL_VERSION,
                    direction,
                    entry_ask,
                    config::max_entry_price(),
                    market.slug,
                    secs_in,
                );
                continue;
            }

            if state.scorer_mode != ScorerMode::Active {
                // EV edge filter — confidence must exceed entry price + slippage
                let entry_price_with_slippage = entry_ask + config::SLIPPAGE;
                edge = signal.confidence - entry_price_with_slippage;
            }

            if state.scorer_mode != ScorerMode::Active && edge < config::min_edge() {
                state.update_stats(|s| s.skip_low_edge += 1);
                info!(
                    "⛔ {} SKIP (low edge): {} edge={:.4} < {:.2} (conf={:.3}, price={:.4}) on {} @ {}s",
                    version::SIGNAL_VERSION,
                    direction, edge, config::min_edge(),
                    signal.confidence, entry_ask + config::SLIPPAGE,
                    market.slug, secs_in,
                );
                continue;
            }

            if state.scorer_mode == ScorerMode::Active && edge <= 0.0 {
                state.update_stats(|stats| stats.skip_calibrated_no_ev += 1);
                continue;
            }

            if config::enable_volume_gate() {
                if let Some(median_hourly_volume) = state.volume_median() {
                    let window_volume: f64 = trades.iter().map(|t| t.quantity).sum();
                    let observed_duration = (secs_in.max(1)) as f64;
                    let observed_hourly_rate = hourly_volume_rate(window_volume, observed_duration);
                    if observed_hourly_rate < median_hourly_volume {
                        state.update_stats(|s| s.skip_volume_gate += 1);
                        info!(
                            "⛔ {} SKIP (volume gate): hourly_rate={:.2} < median={:.2} on {} @ {}s",
                            version::SIGNAL_VERSION,
                            observed_hourly_rate,
                            median_hourly_volume,
                            market.slug,
                            secs_in,
                        );
                        continue;
                    }
                }
            }

            let candidate = BestEntryCandidate {
                direction,
                confidence: candidate_confidence,
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
                ranking_score,
                ranking_basis: ranking_basis.clone(),
                entry_ask,
                entry_bid,
                secs_in,
                n_trades: trades.len(),
                scoring_mode: state.scorer_mode.as_str().to_string(),
                raw_model_prob_up: calibrated_ctx.as_ref().map(|ctx| ctx.raw_model_prob_up),
                calibrated_prob_up: calibrated_ctx.as_ref().map(|ctx| ctx.calibrated_prob_up),
                selected_side_prob: calibrated_ctx.as_ref().and_then(|ctx| {
                    ctx.selected
                        .as_ref()
                        .map(|selection| selection.selected_side_prob)
                }),
                ev_up: calibrated_ctx.as_ref().and_then(|ctx| ctx.ev_up),
                ev_down: calibrated_ctx.as_ref().and_then(|ctx| ctx.ev_down),
                artifact_version: calibrated_ctx
                    .as_ref()
                    .map(|ctx| ctx.artifact_version.clone()),
            };

            if state.consider_best_candidate(candidate.clone()) {
                info!(
                    "⭐ v14 best candidate updated: {} conf={:.3} edge={:.4} rank={}({:.4}) ask={:.4} @ {}s",
                    candidate.direction,
                    candidate.confidence,
                    candidate.edge,
                    candidate.ranking_basis,
                    candidate.ranking_score,
                    candidate.entry_ask,
                    candidate.secs_in,
                );

                emit_best_candidate(&state, &market, now_ms);
            }
        }
    }
}
