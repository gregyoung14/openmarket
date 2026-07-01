# V8: Smart Value (EV Filter)

## Overview

Based on the v6 live trading ledger, we identified a critical flaw in previous versions: **Negative Risk/Reward**. We were winning 58% of trades but losing more money on losses than we gained on wins, because we were buying expensive contracts ($0.60-$0.80) with only moderate confidence.

v8 introduces **Expected Value (EV) Filtering**. It shifts from "Directional Trading" to "Value Trading".

**File:** `backtest_drift_v8.py`
**Status:** PRODUCTION CANDIDATE

## The Problem (v6 Ledger Analysis)
- **Avg Win:** $0.96
- **Avg Loss:** $1.21
- **Profit Factor:** 1.09 (Barely Profitable)
- **Cause:** Buying high-priced contracts ($0.64) with only 60-65% confidence.
   - Cost $0.64 → Reward $0.36.
   - Breakeven Win Rate needed: 64%.
   - Actual Win Rate: ~58%.
   - Result: Negative EV.

## The Solution (v8 Logic)

We implement three strict filters:

### 1. The Edge Filter (EV Check)
We only trade if the model's confidence exceeds the market price by a margin.
```python
Edge = Confidence - (Entry_Price + Slippage)
if Edge < 0.05:
    SKIP_TRADE
```
*Example:* If Price is $0.60, Model Confidence must be > 65%.

### 2. Price Cap
Hard limit on entry price to prevent "picking up pennies in front of a steamroller".
```python
MAX_ENTRY_PRICE = 0.75
```
(Previous versions allowed up to $0.99).

### 3. Drift Refinement (Inherited from v7)
Uses v7's **45s Confirmation Window** and **0.45 Drift Weight** to ensure the direction signal is robust before we even check the price.

## Expected Outcome
- **Fewer Trades:** We will skip many "winning" trades that were too expensive.
- **Higher Profit Factor:** Our average loss should decrease significantly.
- **Better Risk Profile:** We avoid the 5:1 risk scenarios (betting $0.83 to make $0.17).

## How to Run
```bash
python strategies/v8_smart_value/backtest_drift_v8.py
```
