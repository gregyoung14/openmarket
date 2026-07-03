# Feature Contract: Calibrated Net-EV Binary Scorer

**Status:** Implemented First Pass  
**Date:** 2026-04-06  
**Scope:** Paper tournament only for now. Real-money live trading remains off for this scorer path.

---

## 1. Rollout Modes

- `disabled`: signal-engine uses the existing heuristic path only.
- `shadow`: signal-engine keeps heuristic entries live in paper, but also computes calibrated posterior and EV fields every 5 seconds for logging and diagnostics.
- `active`: signal-engine chooses side by calibrated EV every 5 seconds, but this mode is currently intended only for paper-tournament strategies.

The live execution-engine is not part of this rollout. The calibrated scorer is only wired into paper-tournament signal-engine instances via environment variables.

---

## 2. Artifact Location

Default runtime artifact path:

- `data/ml_artifacts/latest_binary_model.json`

Runtime override:

- `CALIBRATED_MODEL_PATH=/abs/path/to/artifact.json`

Artifact freshness monitoring:

- `scripts/monitoring/check_calibrated_artifact.sh`

---

## 3. Dataset Contract

Export endpoint:

- `GET /export/step3_binary_calibration`

Supported query params:

- `start_ts_ms`
- `end_ts_ms`
- `lookback_hours`
- `market_limit`

Primary output:

- `data/ml_exports/step3_binary_calibration_<ts>.csv`

Manifest output:

- `data/ml_exports/step3_binary_calibration_<ts>.manifest.json`

Row cadence:

- 1 row every 5 seconds
- first emitted row at `15s` into market
- last emitted row before market close

Target label:

- `label_up_final = 1` if final Binance close within the 15-minute market is above the market-open price
- ties are dropped from the export

---

## 4. Metadata Columns

- `market_slug`
- `market_start_ms`
- `market_end_ms`
- `ts_ms`
- `market_open_price`
- `market_close_price`
- `label_up_final`

---

## 5. Feature Columns

The first-pass implemented feature list is:

- `secs_in`
- `secs_left`
- `price_vs_open`
- `ret_15s`
- `ret_30s`
- `ret_60s`
- `ret_180s`
- `rv_15s`
- `rv_30s`
- `rv_60s`
- `rv_180s`
- `volume_15s`
- `volume_30s`
- `volume_60s`
- `volume_180s`
- `imbalance_15s`
- `imbalance_30s`
- `imbalance_60s`
- `imbalance_180s`
- `trade_count`
- `trades_per_sec`
- `combined_prob_up`
- `drift_prob_up`
- `signal_confidence`
- `path_eff`
- `autocorr`
- `ofi_accel`
- `adaptive_confirm`
- `vol_1s`
- `regime_trend`
- `regime_neutral`
- `regime_chop`
- `up_best_bid`
- `up_best_ask`
- `down_best_bid`
- `down_best_ask`
- `up_spread`
- `down_spread`
- `sum_bid`
- `sum_ask`
- `mid_up`
- `mid_down`
- `market_mid_prior_up`

These columns are emitted by the recorder export, used by the trainer to define the artifact feature order, and reconstructed at runtime inside signal-engine from the same live state inputs.

---

## 6. Runtime Scoring Rule

The model outputs one posterior:

- `p_up(t)`

Then the runtime derives:

- `EV_up`
- `EV_down`

using the artifact fee and slippage assumptions.

Selection rule:

1. compute calibrated `p_up`
2. compute `EV_up` and `EV_down`
3. if both are `<= CALIBRATED_MIN_EV`, skip
4. otherwise choose the higher positive EV side

The runtime still applies paper safety bounds such as minimum and maximum entry price.

---

## 7. Paper Logging Fields

Paper executor CSV rows now preserve:

- `scoring_mode`
- `ranking_basis`
- `ranking_score`
- `raw_model_prob_up`
- `calibrated_prob_up`
- `selected_side_prob`
- `ev_up`
- `ev_down`
- `artifact_version`

This allows paper runs to be segmented by heuristic versus calibrated selection behavior without enabling real-money trading.

---

## 8. Deferred Items

- `2m` feature family remains deferred.
- Tree-based runtime models remain deferred.
- Real-money live rollout remains deferred.
- Promotion gates still require more archived-data evaluation and paper sample accumulation.
