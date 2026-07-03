# TDR: Calibrated Net-EV Binary Scorer

**Status:** Proposed  
**Date:** 2026-04-05  
**Primary Components:** `rust-services/signal-engine`, `rust-services/market-data-recorder`, `scripts/ml/export_and_train.py`

**Implementation Backlog:** [docs/TASKLIST-calibrated-net-ev-binary-scorer-2026-04-06.md](./TASKLIST-calibrated-net-ev-binary-scorer-2026-04-06.md)

---

## 1. Executive Summary

The deeper fix is not to bias the system toward `UP` or `DOWN` globally.

The deeper fix is to replace the current ad hoc confidence-plus-price heuristic with a **single symmetric probability model**:

$$
p_{up}(t) = P(\text{BTC market closes UP} \mid x_t)
$$

where $x_t$ is the feature vector available at time $t$ inside the current 15-minute market.

Then compute net expected value for both sides from the same posterior:

$$
p_{down}(t) = 1 - p_{up}(t)
$$

$$
EV_{up,\$} = \frac{p_{up}(1-f)}{a_{up}+s} - (1+f)
$$

$$
EV_{down,\$} = \frac{(1-p_{up})(1-f)}{a_{down}+s} - (1+f)
$$

with:

- $f$ = fee rate
- $s$ = slippage added to ask price
- $a_{up}, a_{down}$ = current Polymarket ask for each side

That gives one mathematically consistent rule:

1. estimate one posterior for final market direction,
2. convert that posterior into `UP` and `DOWN` EVs,
3. take the side with positive EV if one exists,
4. otherwise skip.

That is not a structural bias toward either side. It is a symmetric decision rule over a 50/50 prior.

---

## 2. Clarifying the Constraint

The user's constraint is correct:

- the market is structurally binary,
- the model should not be hard-coded to favor `UP` or `DOWN`,
- and future predictions should come from math, technical indicators, and stored history.

That means the next system should **not** do this:

- add a permanent `DOWN-only` or `UP-only` live rule,
- optimize separately around an accidental recent side skew,
- or use side asymmetry from a short sample as a first principle.

Instead, the correct framing is:

- prior at market start: roughly 50/50,
- posterior during the market: feature-driven and time-varying,
- side selection: derived from the posterior and current contract asks.

So the right model is **one symmetric binary classifier**, not two unrelated direction-specific heuristics.

---

## 3. Why Raw Edge Is the Wrong Ranking Function

The current signal engine effectively uses:

$$
edge = confidence - (ask + slippage)
$$

and then picks the best candidate by raw `edge` first.

This is flawed for two reasons.

### 3.1 `confidence` is not calibrated

The current `confidence` is the output of a weighted drift heuristic composed of:

- Brownian drift posterior,
- OFI acceleration,
- scoreboard vs open,
- whipsaw dampening,
- regime penalties,
- adaptive confirmation.

This is a reasonable signal generator, but it is **not** yet a calibrated probability estimate.

So subtracting price from it does not produce a reliable economic edge.

### 3.2 Raw edge mechanically favors cheap contracts

If two contracts have similar model confidence, the cheaper contract will always show larger raw edge.

That means ranking by edge first introduces an unintended bias toward low-price contracts even before we know whether low-price contracts are actually the best bets in live data.

This is exactly the failure mode already observed in the paper tournament.

---

## 4. What Data We Already Store

The good news is that the repository already stores most of what is needed for a real calibration pipeline.

### Live DB contents already available

From the current SQLite and recorder stack, we already have:

- `binance_trades`
- `binance_ticks_ms`
- `polymarket_ticks_ms`
- `lag_pairs_ms`
- `binance_candles_1s`
- `binance_candles_5s`
- `binance_candles_1m`
- `binance_candles_5m`
- `binance_candles_15m`
- `market_meta`

### Recorder export machinery already exists

The market-data-recorder exposes:

- `GET /export/step1`
- `GET /export/step2`
- `GET /export/step2_hf`

### Existing exported features already include real technical indicators

The 15-minute export already contains:

- returns and momentum:
  - `ret_1`, `ret_2`, `ret_4`, `roc_3`, `roc_6`, `mom_3`, `mom_6`
- moving-average structure:
  - `ema_9`, `ema_21`, `ema_50`, `ema_12`, `ema_26`, `ema_slope_9`, `ema_slope_21`, `ema_cross`
- oscillator indicators:
  - `rsi_14`, `rsi_30`, `stoch_k_14`, `stoch_d_3`, `cci_20`
- volatility and range:
  - `rv_1s`, `rv_5s`, `atr_14`, `bb_width_20`, `bb_squeeze_20`, `range_pct`
- flow and volume:
  - `buy_sell_imbalance`, `volume_z_20`, `quote_volume_z_20`, `obv`, `obv_delta`, `burstiness`
- Polymarket microstructure:
  - `up_bid_last`, `up_ask_last`, `down_bid_last`, `down_ask_last`, `up_spread`, `down_spread`, `sum_bid`, `sum_ask`, `mid_up`, `mid_down`
- cross-venue timing:
  - `lag_mean_ms`, `lag_abs_mean_ms`, `up_bid_drift_*`, `down_bid_drift_*`

### High-frequency export already exists

The HF exporter also adds:

- per-bucket microstructure rows at `100ms` and `1s`
- 15-minute context values:
  - `ctx_15m_close`
  - `ctx_15m_ret_1`
  - `ctx_15m_ema_21`
  - `ctx_15m_ema_50`
  - `ctx_15m_regime_up`
  - `ctx_15m_lag_mean_ms`
  - `ctx_15m_sum_bid`

So the data problem is not “we have nothing.”

The real issue is that the current exports are not yet aligned to the exact binary market-close prediction target we need.

---

## 5. What We Learned from the Current Pipeline

### 5.1 The recorder can export 15-minute features now

Today, `GET /export/step2` completed successfully and wrote:

- `data/ml_exports/step2_features_15m_1775414936201.csv`

### 5.2 The high-frequency exporter is too heavy in its current form

Today, `GET /export/step2_hf` did not complete within the probe window and hung long enough that it had to be terminated.

That means the HF export path is currently an offline research tool, not something we can depend on as a fast interactive loop.

### 5.3 The live DB is too short for robust calibration right now

Current live DB span:

- `binance_ticks_ms`: `2026-04-03 17:23:39` → `2026-04-05 19:15:24`
- `polymarket_ticks_ms`: `2026-04-03 17:18:42` → `2026-04-05 19:15:27`

That is only about two days of live history after the recovery/reset.

### 5.4 The small current 15-minute sample does not support confidence yet

A quick time-split logistic regression on the freshly exported 15-minute file produced:

- rows: `199`
- test rows: `79`
- AUC-ROC: `0.4922`
- Brier: `0.3195`
- ECE: `0.2717`

Interpretation:

- this sample is too small and too weak to justify live scoring changes,
- and the current 15-minute export alone is not the finished answer.

This does **not** mean math-and-indicators cannot work. It means the current training slice is too short and the target is not yet aligned to the exact market-close decision problem.

---

## 6. The Correct Prediction Target

The current offline script is optimized around short-horizon labels like:

- `target_h1s_up`
- `target_h5s_up`

That is useful for microstructure research, but the paper tournament currently holds positions to market resolution.

So the correct live target is not:

- “will the next 1s or 5s price move up?”

The correct live target is:

$$
y_t = \mathbb{1}[\text{BTC close at market end} > \text{BTC open at market start}]
$$

for each timestamp $t$ inside the same market.

### Practical labeling rule

For each market slug `btc-updown-15m-<epoch_start>`:

- `market_start_ms = epoch_start * 1000`
- `market_end_ms = market_start_ms + 900000`
- `market_open_price = first Binance price at or after market_start_ms`
- `market_close_price = last Binance price at or before market_end_ms`
- `label_up = 1 if market_close_price > market_open_price else 0`

Then emit one row per second or per scan tick inside that market with:

- feature vector at time $t$
- `secs_in`
- `secs_left`
- current Polymarket asks/bids
- final binary label `label_up`

This is the exact dataset needed for calibration of the live strategy.

---

## 7. The Right Model Structure

### 7.1 One symmetric posterior

The live model should output only one quantity:

$$
p_{up}(t)
$$

Then derive:

$$
p_{down}(t) = 1 - p_{up}(t)
$$

This preserves symmetry and avoids hard-coded directional bias.

### 7.2 Feature families to include

The model should use four layers of information.

#### A. Exact signal-engine features

These should be exported or reconstructed offline using the same logic as `compute_drift_signal_v14()`:

- `combined_prob_up`
- `drift_prob_up`
- `ofi_accel`
- `scoreboard`
- `path_eff`
- `autocorr`
- `vol_1s`
- `consistency`
- `adaptive_confirm`
- regime state

This is critical because the calibration problem is specifically about turning current signal-engine beliefs into true probabilities.

#### B. Technical indicators from stored BTC data

Already available or straightforward from the recorder:

- returns / momentum
- EMA slopes and cross state
- MACD and histogram
- RSI / stochastic / CCI
- ATR / realized volatility / Bollinger width
- OBV / volume imbalance / burstiness

#### C. Polymarket microstructure

- current `UP` and `DOWN` best bid / ask
- spread and total bid / ask structure
- mid-price imbalance
- bid drift and lag metrics
- implied market prior from mid prices

#### D. Time and market context

- `secs_in`
- `secs_left`
- prior 15-minute regime and lag context
- current market price relative to open
- whether current price is near price-floor or price-cap zones

### 7.3 Base model choice

Start with a simple, auditable classifier:

1. logistic regression as the first baseline,
2. then gradient-boosted trees only if logistic clearly underfits.

Reason:

- logistic regression is fast,
- easy to port or serialize,
- easy to calibrate,
- and easier to debug when live probabilities drift.

---

## 8. Calibration Layer

The model score should not be used directly.

Use a calibration layer trained on out-of-sample predictions.

### Recommended calibration order

1. train base model on historical feature rows,
2. produce walk-forward out-of-sample probabilities,
3. fit calibration map on those out-of-sample predictions,
4. store calibration artifact with the base model.

### Candidate calibrators

- Platt scaling for a simple first pass
- isotonic regression if probability monotonicity is present but nonlinear

### Important nuance: time-to-close matters

The same raw score at `20s` into market and `540s` into market should not necessarily map to the same final outcome probability.

Recommended approach:

- either include `secs_in` and `secs_left` as features in one global calibrator,
- or fit separate calibration buckets by time bands, such as:
  - `15-60s`
  - `60-180s`
  - `180-360s`
  - `360-600s`

---

## 9. Net EV Scoring Rule

After calibration, score the market symmetrically.

Let:

- $p = p_{up}(t)$
- $f$ = fee rate
- $s$ = slippage in price units
- $a_{up}$ = current `UP` ask
- $a_{down}$ = current `DOWN` ask

Then:

$$
EV_{up,\$} = \frac{p(1-f)}{a_{up}+s} - (1+f)
$$

$$
EV_{down,\$} = \frac{(1-p)(1-f)}{a_{down}+s} - (1+f)
$$

### Runtime rule

1. compute calibrated $p_{up}$
2. compute `EV_up` and `EV_down`
3. if both are `<= 0`, skip
4. otherwise choose the higher positive EV

### Why this preserves symmetry

This rule does not assume one side is better in general.

It only says:

- use one balanced posterior,
- compare both contracts under the same economics,
- take the positive opportunity if it exists.

Over many markets, a well-calibrated model should not be permanently `UP`-biased or `DOWN`-biased unless the data actually supports that posterior at those times.

---

## 10. Why This Is Better Than Raw Edge

Raw edge answers:

- “How far above price does the heuristic confidence sit?”

Calibrated EV answers:

- “After fees, slippage, and payout asymmetry, how many dollars of expected value does this contract offer per dollar risked?”

That is the correct optimization target.

It automatically handles the fact that:

- cheap contracts can still be bad if the posterior is wrong,
- expensive contracts can still be good if the posterior is strong enough,
- and contract price must be judged relative to calibrated probability, not heuristic score alone.

---

## 11. What Needs To Change in Code

### `rust-services/market-data-recorder`

Add a new export path for binary-outcome calibration rows.

Required new output:

- one row per second or scan tick inside each market,
- exact market-close binary label,
- current Polymarket book state,
- exact signal-engine-compatible feature columns.

Potential new endpoint:

- `GET /export/step3_binary_calibration`

### `scripts/ml/`

Add a new training script specifically for the binary outcome model.

Suggested file:

- `scripts/ml/train_binary_outcome_model.py`

This script should:

- load the new export,
- run walk-forward training,
- fit calibration,
- report AUC / Brier / ECE / EV metrics,
- write a compact artifact for runtime loading.

### `rust-services/signal-engine`

Add:

- model artifact loading at startup,
- runtime feature vector assembly,
- calibrated posterior computation,
- EV scoring for both sides,
- candidate ranking by EV instead of raw edge.

### Suggested artifact shape

Keep the first artifact simple:

- feature list in fixed order
- logistic weights + intercept
- standardization means / stds
- calibration table or isotonic bins
- metadata: train window, Brier, ECE, timestamp

This can live as a JSON file checked into deployment artifacts or loaded from disk.

---

## 12. Operational Reality: Use Archived Snapshots

Because the live DB currently only spans about two days, proper training should not rely on the current VPS database alone.

Use the archived CDN snapshots documented in [docs/TDR-backtest-database-access.md](./TDR-backtest-database-access.md).

That gives:

- materially larger sample size,
- enough markets to calibrate probabilities,
- and enough regime diversity to avoid overfitting to the current post-recovery window.

Without that, any “calibrated EV” model we train today will be mostly noise.

---

## 13. Recommended Validation Criteria

Before a calibrated EV scorer is trusted in paper, require all of these on walk-forward out-of-sample data:

1. Brier materially below naive baseline
2. ECE below `0.05` to `0.08`
3. positive EV on out-of-sample simulated trades after fees/slippage
4. performance not dominated by a tiny subset of markets
5. no dependence on one side being structurally favored

That last criterion matters for the user's stated constraint.

If the model only works by leaning permanently `DOWN` or `UP`, it is not the right model.

---

## 14. Final Recommendation

The deeper fix is:

1. stop treating current `confidence` as a price-ready probability,
2. train a symmetric market-close posterior from stored history,
3. calibrate it out-of-sample,
4. compute `UP` and `DOWN` EV from the same posterior,
5. rank candidates by net EV, not raw edge.

The repo is already close to supporting this:

- the recorder stores the right raw data,
- the offline script already understands walk-forward, Brier, and ECE,
- and the signal engine already exposes the right conceptual components.

What is missing is the glue:

- the correct binary-outcome label export,
- exact signal-engine feature reconstruction,
- and a runtime model artifact for calibrated EV scoring.

That is the mathematically sound path to a future prediction engine that uses technical indicators and stored lookback data without hard-coding a side bias.
