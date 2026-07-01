# V7: Drift Refinement

## Overview

Refined version of v6 Drift Estimator, optimized based on the strong performance on 47 markets. v7 tightens the signal criteria to focus on "high conviction" setups and reduce noise even further.

**File:** `backtest_drift_v7.py`
**Status:** EXPERIMENTAL (Refinement of v6)

## Key Changes vs v6

| Parameter | v6 (Original) | v7 (Refined) | Rationale |
|-----------|---------------|--------------|-----------|
| **Confirmation Window** | 30s | **45s** | Reduce false positives in choppy markets; v6 winners were very stable. |
| **Drift Weight** | 0.40 | **0.45** | The Brownian drift component is the strongest predictor; increased its influence. |
| **EMA Weight** | 0.15 | **0.10** | EMA is a lagging indicator; reduced weight to favor real-time drift & order flow. |
| **Min Confidence** | 0.60 | **0.65** | v6 showed <65% trades were breakeven; focusing capital on >65% setups. |
| **Confidence Sweep** | 0.55-0.80 | **0.65-0.85** | Shifts analysis to the higher-probability zone. |

## Hypothesis

By increasing the confirmation window to 45s and slightly boosting the pure drift signal, we aim to:
1.  **Increase Win Rate** from ~70% to >72% by filtering out short-lived noise spikes.
2.  **Reduce Drawdown** by avoiding "flip-flopping" signals in low-volatility regimes.
3.  **Maintain ROI** even with fewer trades, because the win rate on taken trades should be higher (Kelly criterion logic).

## Strategies Tested

Same as v6:
- **Hold-to-Resolve:** Primary strategy.
- **Momentum (TP=10%):** Secondary strategy (scalping).

## How to Run

```bash
python strategies/v7_drift_refinement/backtest_drift_v7.py
```

## Expected Outcome

We expect slightly fewer trades than v6 (due to the 45s window and higher confidence floor), but a higher win rate and sharper Equity Curve.
