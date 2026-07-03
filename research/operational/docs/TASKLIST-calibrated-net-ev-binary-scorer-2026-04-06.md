# Task List: Calibrated Net-EV Binary Scorer Delivery

**Status:** Proposed  
**Date:** 2026-04-06  
**Primary Components:** `rust-services/market-data-recorder`, `scripts/ml`, `rust-services/signal-engine`, `rust-services/paper-executor`, `services/paper-tournament`

---

## 0. Implementation Status

Implemented on 2026-04-06:

- New recorder export: `GET /export/step3_binary_calibration`
- New offline trainer: `scripts/ml/train_binary_outcome_model.py`
- Stable runtime artifact path: `data/ml_artifacts/latest_binary_model.json`
- Signal-engine calibrated scorer integration with `disabled`, `shadow`, and `active` modes
- Paper-executor logging for calibrated scorer metadata
- Paper-tournament variants for calibrated `shadow` and paper-only `active` modes
- Artifact freshness monitoring and calibrated scorer health visibility
- Feature-contract documentation and archived snapshot bootstrap script

Validated on the current environment:

- `cargo check` passed for `signal-engine`
- `cargo check` passed for `market-data-recorder`
- `cargo check` passed for `paper-executor`
- Direct smoke test passed for the new step-3 export
- First local artifact was generated at `data/ml_artifacts/latest_binary_model.json`
- Isolated signal-engine runtime smoke test confirmed the artifact loads in `shadow` mode

Still dependent on future data accumulation or research iteration:

- archived snapshot training beyond the current local smoke pass
- broader ablation work across cadence and feature families
- longer paper-tournament comparison history before any promotion decision
- any real-money live rollout remains explicitly out of scope for now

---

## 1. Goal

Replace the current raw-edge-first candidate ranking with a symmetric calibrated expected-value scorer for 15-minute BTC up/down markets.

The target operating shape for the first implementation is:

- raw Binance trades and Polymarket ticks remain the source of truth,
- 1-second state remains the live feature base,
- model rows are emitted every 5 seconds,
- 30-second, 1-minute, and 3-minute context is derived from the 1-second base,
- one posterior `p_up(t)` is estimated and used to derive EV for both sides,
- `2m` features stay out of scope unless ablation later proves they add signal.

---

## 2. Definition of Done

The project is complete only when all of the following are true:

- A repeatable export exists for final market-close binary labels at 5-second cadence.
- Archived snapshot data can be used to train and evaluate the model on materially more than the current live two-day window.
- A reproducible training pipeline produces a compact runtime artifact plus calibration metadata.
- The signal engine can load that artifact, assemble live features, compute symmetric EV for both sides, and rank candidates by EV rather than raw edge.
- The paper tournament can compare the calibrated scorer against the current heuristic path in shadow or active mode.
- Monitoring exists for export health, artifact freshness, and scorer runtime behavior.
- Walk-forward metrics and paper results clear explicit promotion gates before live consideration.

---

## 3. Phase 0: Baseline, Ownership, and Guardrails

Primary components: `docs`, `rust-services/signal-engine`, `services/paper-tournament`

- [ ] Freeze the current baseline reference numbers from the v2 tournament, including baseline, canary, win rate, PnL, profit factor, and price-bucket behavior.
- [ ] Freeze the economic constants used for research and runtime scoring: fee rate, slippage assumption, max price handling, and any minimum EV threshold.
- [ ] Decide the rollout modes up front: `disabled`, `shadow`, `active`.
- [ ] Decide where trained artifacts live on disk, how they are versioned, and how the signal engine discovers them.
- [ ] Create a single source-of-truth feature-contract note that defines required columns, units, null policy, and ordering.
- [ ] Decide which component owns shared feature definitions so offline and runtime logic do not drift.

Exit criteria:

- Baseline metrics are written down.
- Artifact location and runtime mode flags are fixed.
- The feature contract is frozen before export and trainer work begins.

---

## 4. Phase 1: Historical Data Foundation

Primary components: `docs`, `rust-services/db-backup`, archived CDN snapshots

- [ ] Inventory all archived database snapshots and map their date coverage.
- [ ] Estimate local disk, CPU, and scratch-space requirements for snapshot download, decompression, and export jobs.
- [ ] Create a repeatable snapshot bootstrap script or documented command sequence for download, integrity check, and read-only access.
- [ ] Verify schema compatibility across snapshots so the export path will not fail on older archives.
- [ ] Select initial training, validation, and holdout periods that span multiple market regimes rather than just the current post-recovery window.
- [ ] Measure how long export work takes on one representative archived snapshot and record the operational envelope.

Exit criteria:

- At least one archived snapshot can be queried locally.
- Training windows are chosen from archived data, not just the live VPS database.
- The team knows whether export jobs need chunking by date or market for performance.

---

## 5. Phase 2: Label and Dataset Contract

Primary components: `docs`, `rust-services/market-data-recorder`, `scripts/ml`

- [ ] Codify the exact market labeling rule for the final binary outcome, including how market start and end are derived from the slug.
- [ ] Define how market open price and market close price are selected from Binance data when the exact timestamp is missing.
- [ ] Decide how ties are handled and whether tied markets are dropped or labeled explicitly.
- [ ] Define exclusion rules for corrupted markets, incomplete book state, missing Binance coverage, or very-late rows.
- [ ] Freeze the first dataset cadence at 5-second rows derived from 1-second state.
- [ ] Freeze the mandatory time-context columns, including `secs_in`, `secs_left`, and price relative to market open.
- [ ] Freeze the first feature window set: 1-second base plus 15-second, 30-second, 1-minute, and 3-minute derived context.
- [ ] Explicitly defer `2m` unless later ablation shows incremental value.
- [ ] Decide whether `5m` enters the first pass as an optional slow-anchor feature or stays deferred.

Exit criteria:

- The dataset spec is stable enough that export code, trainer code, and runtime code can all target the same contract.

---

## 6. Phase 3: Recorder Export Implementation

Primary components: `rust-services/market-data-recorder`

- [ ] Add a new export path for binary-outcome calibration rows, for example `GET /export/step3_binary_calibration`.
- [ ] Implement market parsing helpers that map each slug to `market_start_ms`, `market_end_ms`, and canonical market identity.
- [ ] Build a per-market timeline from the 1-second base state and emit rows every 5 seconds.
- [ ] Add final target columns, including the exact binary label and the market open/close prices used to create it.
- [ ] Add current Polymarket book fields needed for EV scoring: best bid, best ask, spread, mid, imbalance, depth summaries, and implied prior.
- [ ] Add exact or shared signal-engine-compatible features so the calibration dataset matches live candidate scoring inputs.
- [ ] Add BTC technical features derived from 1-second state plus 15-second, 30-second, 1-minute, and 3-minute windows.
- [ ] Add time-state features such as `secs_in`, `secs_left`, current price versus market open, and any price-cap proximity flags.
- [ ] Add date-range, market-count, and chunking controls so this exporter does not have the same usability problem as the current heavy HF export.
- [ ] Add progress logging and a small output manifest so long exports are observable.
- [ ] Add unit or integration tests against a small fixture database that validate row count, label correctness, and key feature columns.

Exit criteria:

- The new export completes on at least one archived snapshot without hanging.
- Output rows match the frozen dataset contract.
- Label correctness is covered by tests.

---

## 7. Phase 4: Offline Training Pipeline

Primary components: `scripts/ml`

- [ ] Add a dedicated trainer, such as `scripts/ml/train_binary_outcome_model.py`.
- [ ] Load the new step-3 export and validate schema at startup.
- [ ] Split data by market and time, not by random rows, to avoid leakage.
- [ ] Build the first baseline with standardized logistic regression.
- [ ] Add a second optional base model path only if needed later, such as gradient-boosted trees.
- [ ] Fit walk-forward out-of-sample probabilities.
- [ ] Add calibration options in priority order: Platt first, isotonic second.
- [ ] Support either one global calibrator with time-left features or bucketed calibrators by time-to-close.
- [ ] Report metrics that matter for this strategy: Brier, ECE, log loss, AUC, EV capture, hit rate of positive-EV trades, and side distribution.
- [ ] Emit a compact runtime artifact that includes feature order, normalization parameters, model weights, calibrator data, and training metadata.
- [ ] Write a machine-readable metrics summary alongside the artifact so later promotion gates can be automated.

Exit criteria:

- A single command can train, evaluate, and emit a runtime artifact on archived data.
- Metrics are reproducible and tied to an explicit training window.

---

## 8. Phase 5: Research and Ablation

Primary components: `scripts/ml`, archived snapshots, `docs`

- [ ] Compare 1-second, 5-second, and 15-second row cadences.
- [ ] Compare 1-second-only features against adding 30-second, 1-minute, and 3-minute context.
- [ ] Test whether 15-second context adds enough to keep in the first live artifact.
- [ ] Test whether adding the current signal-engine heuristic features improves calibration versus pure market-data features.
- [ ] Compare global calibration against time-bucket calibration.
- [ ] Compare no-calibration, Platt, and isotonic.
- [ ] Run EV sensitivity sweeps across fee, slippage, and minimum-EV thresholds.
- [ ] Segment results by side, ask bucket, regime, and time remaining to detect hidden asymmetries.
- [ ] Confirm that any observed `UP` or `DOWN` skew is treated as a diagnostic and not turned into a hard-coded bias rule.
- [ ] Decide whether `5m` features are worth adding and whether `2m` can remain deferred.

Exit criteria:

- One first-pass configuration is chosen for runtime integration.
- The choice is backed by out-of-sample metrics and EV behavior, not intuition alone.

---

## 9. Phase 6: Signal Engine Runtime Integration

Primary components: `rust-services/signal-engine`

- [ ] Add config flags for scorer mode, artifact path, artifact version, and fallback behavior.
- [ ] Implement artifact loading at startup with clear failure messages and version checks.
- [ ] Implement runtime feature assembly using the same feature order and transformations as the offline trainer.
- [ ] Keep 1-second state as the live feature base and score on a 5-second cadence.
- [ ] Compute calibrated `p_up`, derive `p_down = 1 - p_up`, and compute `EV_up` and `EV_down` using current asks, fee, and slippage assumptions.
- [ ] Replace raw-edge-first candidate ranking with EV-based ranking, ideally behind a feature flag so the heuristic path remains available during rollout.
- [ ] Add structured logs for posterior, both EVs, chosen side, skip reason, and artifact version.
- [ ] Add tests for artifact loading, normalization, probability output, EV math, and candidate selection.
- [ ] Benchmark runtime cost so the scorer does not introduce unacceptable latency or CPU load.

Exit criteria:

- The signal engine can run in shadow mode and produce stable calibrated scores without affecting existing behavior.

---

## 10. Phase 7: Paper Tournament Integration and Comparison

Primary components: `rust-services/paper-executor`, `services/paper-tournament`, `docs/monitoring`

- [ ] Extend paper logs to record artifact version, posterior, both EVs, selected side, and skip reason.
- [ ] Add a shadow-comparison mode so the tournament can record what the calibrated scorer would have done without changing the executed path.
- [ ] Add at least one calibrated strategy variant to the tournament launcher once shadow scoring is stable.
- [ ] Update the dashboard or comparison endpoint so calibrated variants can be separated cleanly from heuristic variants.
- [ ] Add daily or per-run summary outputs for calibration drift, EV realization, and trade-bucket behavior.
- [ ] Review whether the calibrated scorer is still falling into the cheap-contract trap identified in the canary analysis.

Exit criteria:

- The paper tournament can compare heuristic and calibrated selection on the same reporting surface.
- The calibrated scorer has enough shadow or paper history to judge behavior, not just offline metrics.

---

## 11. Phase 8: Monitoring and Operations

Primary components: `scripts/monitoring`, `systemd/user`, `docs/monitoring`

- [ ] Add artifact-freshness checks so stale model files are visible before runtime drift becomes a hidden problem.
- [ ] Add health checks for export completion, trainer completion, and artifact publication.
- [ ] Add scorer-specific runtime checks for missing features, invalid normalization values, or impossible probability outputs.
- [ ] Add alerts for the signal engine falling back from calibrated mode to heuristic mode.
- [ ] Add log snippets or commands to the monitoring docs so calibration runs and scorer state are easy to inspect.
- [ ] Decide whether model training stays manual, scheduled, or semi-automated after the first stable version.

Exit criteria:

- Operators can tell whether the calibrated stack is healthy without reading code or reverse-engineering logs.

---

## 12. Phase 9: Promotion Gates and Go/No-Go Review

Primary components: `docs`, `scripts/ml`, `services/paper-tournament`

- [ ] Set explicit minimum sample-size requirements for archived-data evaluation and paper-tournament evaluation.
- [ ] Set numeric acceptance gates for calibration quality, including Brier and ECE thresholds.
- [ ] Set numeric acceptance gates for economic quality, including positive net EV after fees and slippage and acceptable concentration by side or ask bucket.
- [ ] Require that the calibrated scorer outperform the current heuristic path in paper on the metrics that actually matter.
- [ ] Review the worst losses and skip decisions manually before any live consideration.
- [ ] Write a short go or no-go note summarizing the evidence, failure modes, and next action.

Exit criteria:

- There is a written promotion decision grounded in both offline and paper evidence.

---

## 13. Recommended Execution Order

1. Finish Phase 0 so the contract, artifact location, and rollout modes are fixed.
2. Finish Phase 1 and Phase 2 before writing heavy export code.
3. Build Phase 3 export code and validate it on archived snapshots.
4. Build Phase 4 trainer code and artifact emission.
5. Run Phase 5 ablations and choose a first runtime configuration.
6. Integrate into the signal engine in shadow mode under Phase 6.
7. Run Phase 7 paper comparisons long enough to observe stable behavior.
8. Add Phase 8 operational checks before trusting the scorer.
9. Use Phase 9 promotion gates before any live rollout discussion.

---

## 14. Explicit Deferrals for the First Pass

- [ ] Do not add a hard-coded `UP` or `DOWN` bias rule.
- [ ] Do not prioritize a 2-minute feature family unless ablation shows clear incremental value.
- [ ] Do not jump to a complex tree-based runtime model until the logistic baseline is exhausted.
- [ ] Do not depend on the current two-day live DB window for calibration.
- [ ] Do not treat the current heavy HF exporter as the required first path if the new 5-second exporter can capture the needed information more cleanly.

---

## 15. Natural First Implementation Slice

If this gets executed incrementally, the most efficient first slice is:

1. Freeze the dataset contract.
2. Add the 5-second binary-outcome export with 30-second, 1-minute, and 3-minute context.
3. Train the first logistic plus calibration artifact on archived snapshots.
4. Run walk-forward Brier, ECE, and EV evaluation.
5. Integrate the scorer in shadow mode before changing candidate selection live.
