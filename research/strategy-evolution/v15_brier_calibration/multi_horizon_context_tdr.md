# TDR: Multi-Horizon Context Layer for BTC/Polymarket Signal Engine

**Date:** 2026-03-19
**Status:** Proposed
**Author:** Greg
**Depends On:** Current Rust signal-engine, current paper-executor, existing fast microstructure signal stack

---

## 1. Executive Summary

The current live strategy stack is optimized for short-horizon signal extraction inside a single 15-minute Polymarket BTC market. It uses fast features well: 1-second bars, intrawindow drift, short regime detection, adaptive confirmation, and entry filters.

What it does **not** currently do in a meaningful way is maintain a higher-timeframe view of BTC market structure across 1 hour, 4 hours, or longer horizons and use that information to decide **when the fast signal should be trusted**.

This TDR proposes a **three-layer strategy architecture**:

1. **Fast Layer**
   Uses the current short-horizon live signal stack with minimal changes.
2. **Context Layer**
   Maintains higher-timeframe BTC state across 5-minute, 15-minute, 1-hour, and 4-hour horizons.
3. **Policy Layer**
   Uses higher-timeframe context to gate, reweight, or resize the fast signal rather than replacing it.

Core thesis:

> The most likely additional edge from higher-timeframe BTC information is not materially better raw directional prediction. The more realistic source of extra edge is improved **trade selection, regime filtering, thresholding, and sizing**.

This means the first version of the system should **not** let slow context directly fire trades. Instead, slow context should tell us whether the current fast signal is operating in a favorable or hostile environment.

---

## 2. Problem Statement

The current fast strategy is good at answering the following question:

> Given the current Polymarket market window and the most recent short-horizon BTC microstructure, is there a tradable edge right now?

It is weaker at answering the following question:

> Is this the kind of broader BTC environment where this fast edge tends to hold up after fees, slippage, and live noise?

This matters because many short-horizon signals degrade in specific higher-timeframe environments:

- Fast momentum often works better when 1-hour and 4-hour trend are aligned.
- Mean-reverting microstructure often performs worse during volatility expansion.
- Thin-liquidity local signals often fail after costs.
- Strong slow-trend environments can make short-term fade signals structurally worse.

The problem is therefore not merely “add more features.”

The real problem is:

> Build a higher-timeframe context layer that improves the **conditional expectancy** of the existing fast strategy without overfitting the system into a noisy macro predictor.

---

## 3. Goals

### 3.1 Primary Goal

Increase net strategy quality by using higher-timeframe BTC context to improve:

- trade selection
- regime avoidance
- dynamic thresholds
- dynamic confirmation behavior
- dynamic sizing

### 3.2 Secondary Goals

- Reduce entries in hostile or low-quality regimes.
- Improve net PnL after slippage and fees.
- Reduce drawdown and bad streak clustering.
- Preserve the existing fast engine and avoid a rewrite-first approach.
- Keep the model simple enough to validate with strong walk-forward testing.

### 3.3 Tertiary Goals

- Produce a reusable context-state subsystem for future BTC strategies.
- Create a paper-trading testbed for ablation and live shadow evaluation.
- Make the architecture straightforward for LLM-assisted iteration.

---

## 4. Non-Goals

This design does **not** aim to:

- replace the current fast signal engine with a pure higher-timeframe directional model
- introduce a large black-box ML model in v1
- add dozens of horizons or dozens of correlated features immediately
- claim that 4-hour context will massively improve raw directional accuracy
- optimize a giant parameter grid before live shadow evidence exists

---

## 5. Current System Summary

Current live architecture in the repo:

```text
Binance WS (8001)
        +
Polymarket WS (8002)
        -> signal-engine (8003)
        -> execution-engine (8004)

Paper mode variant:
signal-engine -> paper-executor
```

Current signal characteristics:

- scans once per second during the active market
- operates inside a single 15-minute Polymarket market window
- uses short regime detection from recent 1-second prices
- uses adaptive confirmation
- applies price cap, confidence, edge, and volume filters

Current strengths:

- clean low-latency implementation
- good short-horizon signal discipline
- already structured for paper validation

Current limitations:

- limited cross-market memory for directional context
- no explicit 1-hour or 4-hour BTC structure layer
- no formal slow-regime classifier feeding threshold/sizing logic

---

## 6. Research Summary

The proposed design is directionally supported by research, but with important caveats.

### 6.1 What Research Supports

- **Momentum exists across many assets and horizons.** Medium-horizon trend can carry information about when short-horizon momentum is more reliable.
- **Volatility clusters.** Volatility regime is often more persistent than directional return and is frequently more useful for filtering than for prediction.
- **Microstructure alpha decays quickly after costs.** Context filters can help preserve fragile edge by keeping the system out of poor conditions.
- **Weakly correlated signals can improve robustness.** The benefit is real only when the added signals are actually incremental.
- **Regime models can help.** They work best when feature sets are stable, low-dimensional, and interpretable.
- **Crypto exhibits trend and volatility clustering.** Slow BTC state is therefore a plausible conditioning variable.

### 6.2 What Research Does Not Support

- “Add more horizons and raw accuracy automatically improves.”
- “A large feature stack is safer than a small one.”
- “Higher-timeframe direction will reliably dominate microstructure features on 15-minute markets.”
- “Complex ML will outperform simple gating without major overfit risk.”

### 6.3 Practical Interpretation

The most credible use of slow context is:

- gating
- threshold adjustment
- confirmation adjustment
- size adjustment
- selective abstention

The least credible first use is:

- letting slow context alone fire entries

---

## 7. Core Design Principle

The design principle is:

> Slow context should **condition** fast alpha before it is allowed to become an entry.

There are two mathematically valid ways to express this.

### 7.1 Score Adjustment Form

$$
\text{trade score} = \text{fast alpha} + \text{context adjustment}
$$

This is useful for analytics and calibration.

### 7.2 Gating Form

$$
\text{Take trade only if fast signal is strong and context regime is favorable}
$$

This is the preferred operational form for v1 because it is simpler, more robust, and less likely to overfit.

---

## 8. Proposed Three-Layer Architecture

## 8.1 Layer 1: Fast Layer

The fast layer remains the current short-horizon live engine.

Inputs:

- 1-second BTC trade bars
- intrawindow buy/sell flow
- current market open price
- current Polymarket best bid/ask

Responsibilities:

- compute short-horizon direction and confidence
- detect short regime
- maintain adaptive confirmation
- propose candidate entries

Outputs:

- direction
- confidence
- consistency
- short regime
- adaptive confirm window
- edge before context conditioning

### 8.2 Layer 2: Context Layer

The context layer maintains higher-timeframe state for BTC independently of any single Polymarket market window.

Inputs:

- rolling BTC 1-second bars
- rolling BTC 1-minute bars
- rolling BTC 5-minute bars
- optional external data later: perp funding, basis, open interest

Responsibilities:

- compute higher-timeframe feature set
- classify broad market regime
- expose context state to the policy layer

Outputs:

- context regime label
- context feature vector
- context confidence or stability score

### 8.3 Layer 3: Policy Layer

The policy layer translates the combination of fast signal plus context regime into execution rules.

Responsibilities:

- allow trade
- deny trade
- adjust min confidence
- adjust confirm window
- adjust max entry price
- adjust size

Outputs:

- final entry decision
- final thresholds used
- final size multiplier
- reason code for logging and analysis

---

## 9. Rolling State Design

The context layer needs its own rolling state buffers, independent from the current-market-only trade buffer.

### 9.1 Required Resolutions

Maintain the following rolling bars:

| Resolution | Retention | Purpose |
|---|---:|---|
| 1 second | 15 to 30 minutes | existing fast features, near-term realized vol |
| 1 minute | 6 to 24 hours | 15m, 30m, 1h, 4h context features |
| 5 minutes | 7 days | volume percentile and broader context summaries |

### 9.2 Why These Resolutions

- 1-second bars remain necessary for the current live signal.
- 1-minute bars are the natural resolution for 15m to 4h context.
- 5-minute bars reduce memory and simplify longer rolling summaries.

### 9.3 Persistence Scope

The context state should persist across Polymarket market boundaries.

This is a major difference from the current fast engine, where the trade buffer is reset at each new market.

### 9.4 Failure Handling

If context buffers are not warm enough:

- do not synthesize or backfill fake values
- mark context as unavailable
- fall back to the base fast strategy
- log the fallback explicitly

---

## 10. Initial Context Feature Set

The first version should stay deliberately small.

Recommended initial feature set:

| Feature | Horizon | Why it exists |
|---|---|---|
| BTC return | 15m | captures short-medium directional drift |
| BTC return | 1h | captures local slow trend |
| BTC return | 4h | captures broader directional backdrop |
| Realized volatility | 30m | captures current risk regime |
| Realized volatility | 4h | captures slower volatility backdrop |
| Path efficiency | 1h | distinguishes trend from noisy travel |
| Trend slope | 1h | measures direction plus strength |
| Distance to VWAP | 1h | measures local extension from fair flow anchor |
| Distance to VWAP | 4h | measures broader extension |
| Distance to daily open | 1d | simple, stable directional context |
| Position in 4h range | 4h | helps detect exhaustion vs breakout location |
| Volume percentile by hour-of-week | rolling | captures structural liquidity environment |

### 10.1 Features Explicitly Deferred From v1

- 2h and 8h duplicate horizons
- large indicator libraries
- RSI and MACD style retail overlays unless they prove incremental
- deep learned embeddings
- large external alt-data expansion

### 10.2 Optional External Inputs for v2+

Only add these after v1 proves useful:

- perp funding rate
- spot-perp basis
- open interest
- liquidation intensity
- stablecoin flow proxies

---

## 11. Feature Definitions

The context feature layer must use clear canonical definitions.

### 11.1 Return

For horizon $h$:

$$
r_h = \ln\left(\frac{P_t}{P_{t-h}}\right)
$$

### 11.2 Realized Volatility

For window $W$:

$$
\sigma_W = \sqrt{\frac{1}{N} \sum_{i=1}^{N}(r_i - \bar r)^2}
$$

### 11.3 Path Efficiency

For prices over a window:

$$
\text{path efficiency} = \frac{|P_{end} - P_{start}|}{\sum |\Delta P| + \varepsilon}
$$

### 11.4 Trend Slope

Use ordinary least squares slope of log price over the window.

### 11.5 Distance to VWAP

$$
\text{dist to VWAP} = \frac{P_t - \text{VWAP}_W}{\text{VWAP}_W}
$$

### 11.6 Position in 4h Range

$$
\text{range position} = \frac{P_t - L_{4h}}{H_{4h} - L_{4h} + \varepsilon}
$$

### 11.7 Volume Percentile by Hour-of-Week

Measure recent realized BTC volume against the empirical distribution of similar hour-of-week buckets.

This is preferable to a naive global threshold because BTC liquidity is strongly time-of-day dependent.

---

## 12. Context Regime Classifier

The first version should classify the higher-timeframe environment into a small set of interpretable states.

Recommended initial labels:

| Regime | Interpretation |
|---|---|
| Trend and liquid | strong directional backdrop, healthy participation |
| Trend but overstretched | trend exists, but price is extended from anchors |
| Chop and high vol | unstable environment, hostile to fragile fast signals |
| Quiet and illiquid | weak opportunity set, higher false positive risk |
| Neutral mixed | no clear slow advantage or disadvantage |

### 12.1 Example Rules for v1

These are intentionally heuristic and interpretable.

**Trend and liquid**

- 1h return and 4h return agree in sign
- 1h path efficiency above threshold
- volume percentile not low
- 30m volatility not extreme

**Trend but overstretched**

- trend conditions hold
- distance to 1h or 4h VWAP is above extension threshold
- price near top or bottom of 4h range

**Chop and high vol**

- 1h path efficiency low
- 30m volatility high
- returns disagree across horizons or alternate frequently

**Quiet and illiquid**

- volume percentile low
- realized vol low
- slow return magnitudes muted

**Neutral mixed**

- none of the above states dominate

### 12.2 Why Use Heuristic Regimes First

- interpretable
- debuggable
- easier to ablate
- less overfit risk
- easier to implement inside Rust services

---

## 13. Policy Layer Behavior

The policy layer is where most expected value should come from.

It should map context regime to execution behavior.

### 13.1 Initial Policy Actions

| Policy Control | Example Adjustment |
|---|---|
| Minimum confidence | raise from 0.60 to 0.66 in hostile regimes |
| Confirmation window | raise from 20s to 40s in noisy regimes |
| Maximum entry price | tighten from 0.55 to 0.48 in weak context |
| Size multiplier | cut by 50% when fast and slow disagree |
| Hard skip | skip trades in clearly hostile context |

### 13.2 Example Regime-to-Policy Mapping

| Context Regime | Trade? | Min Confidence | Confirm Multiplier | Max Price | Size Multiplier |
|---|---|---:|---:|---:|---:|
| Trend and liquid | yes | 0.60 | 0.90x | 0.55 | 1.00x |
| Trend but overstretched | yes, selective | 0.63 | 1.05x | 0.52 | 0.75x |
| Neutral mixed | yes, baseline | 0.60 | 1.00x | 0.55 | 1.00x |
| Quiet and illiquid | mostly no | 0.64 | 1.15x | 0.50 | 0.60x |
| Chop and high vol | usually no | 0.66 | 1.30x | 0.48 | 0.50x |

### 13.3 Fast vs Slow Disagreement Rule

If the fast signal direction disagrees with the dominant 1h and 4h trend:

- do not auto-skip immediately in v1
- reduce size first
- require higher confidence and tighter price discipline

This avoids throwing away potentially useful countertrend fast signals before evidence exists.

---

## 14. Why Gating Should Come Before Directional Fusion

The first implementation should treat context as a **risk filter**, not a new alpha engine.

Reasons:

- most additional value likely comes from conditional expectancy, not new direction signal
- simpler systems are easier to validate
- score fusion is easier to overfit than regime gating
- gating yields clearer diagnostics when a policy change helps or hurts

Recommended order of sophistication:

1. Add context-only logging.
2. Add paper-only gating.
3. Add paper-only threshold and sizing adaptation.
4. Add live shadow mode.
5. Only then consider direct score fusion.

---

## 15. Proposed Implementation Architecture for This Repo

The goal is to bolt this onto the current stack with minimal disruption.

### 15.1 New Components

Proposed additions inside `polymarket-btc-scraper/rust-services/signal-engine`:

- `context.rs`
  Maintains rolling higher-timeframe buffers and feature computation.
- `context_models.rs`
  Canonical structs for context features and context regimes.
- `policy.rs`
  Maps fast signal + context regime to final thresholds and size multipliers.

### 15.2 Existing Components That Change

- `upstream.rs`
  Continue ingesting Binance data, but also feed context aggregators.
- `state.rs`
  Add long-lived context state that persists across market resets.
- `scanner.rs`
  Query context and policy layer before candidate selection and before entry.
- `models.rs`
  Extend outbound signal or entry messages with context fields and reason codes.

### 15.3 Important Design Constraint

The fast market-window buffer and the slow cross-market buffers must remain separate.

The current market-only state should still reset at each new market.
The context state must not reset there.

---

## 16. Data Model Proposal

### 16.1 Context Snapshot

```rust
pub struct ContextSnapshot {
    pub ts_ms: i64,
    pub ret_15m: f64,
    pub ret_1h: f64,
    pub ret_4h: f64,
    pub vol_30m: f64,
    pub vol_4h: f64,
    pub path_eff_1h: f64,
    pub trend_slope_1h: f64,
    pub dist_vwap_1h: f64,
    pub dist_vwap_4h: f64,
    pub dist_daily_open: f64,
    pub range_pos_4h: f64,
    pub volume_pct_hour_of_week: f64,
    pub regime: ContextRegime,
    pub regime_score: f64,
}
```

### 16.2 Policy Decision

```rust
pub struct PolicyDecision {
    pub allow_trade: bool,
    pub min_confidence: f64,
    pub confirm_multiplier: f64,
    pub max_entry_price: f64,
    pub size_multiplier: f64,
    pub reason_code: String,
}
```

### 16.3 Why Explicit Structs Matter

- easier testing
- easier paper logging
- easier dashboard display
- easier future ML export
- easier LLM-assisted iteration

---

## 17. Logging and Observability

This project should be fully observable from day one.

Every prediction and every entry candidate should be able to answer:

- what the fast signal said
- what the context regime was
- what the policy layer did
- why the trade was allowed or rejected

### 17.1 New Fields to Log

- context regime
- regime score
- ret_1h
- ret_4h
- vol_30m
- vol_4h
- dist_vwap_1h
- volume percentile hour-of-week
- policy min confidence
- policy max entry price
- policy size multiplier
- policy reason code

### 17.2 Example Reason Codes

- `ctx_favorable`
- `ctx_hostile_skip`
- `ctx_low_liquidity`
- `ctx_slow_fast_disagree`
- `ctx_overstretched_reduce_size`
- `ctx_unavailable_fallback`

---

## 18. Paper-Trading Evaluation Plan

This design should be validated in paper mode before any live capital routing change.

### 18.1 Comparison Arms

At minimum compare:

1. **Base Fast**
   Current fast signal logic only.
2. **Fast + Context Gate**
   Context only decides skip or allow.
3. **Fast + Context Gate + Policy**
   Context adjusts thresholds and size.

### 18.2 Required Metrics

- net PnL
- return on bankroll
- Sharpe-like stability metric
- max drawdown
- turnover
- trade count
- trades per day
- win rate
- average edge at entry
- profit factor

### 18.3 Diagnostic Metrics

- PnL contribution by context regime
- trade acceptance rate by context regime
- fast-only vs gated trade overlap
- trade outcome for accepted vs rejected fast candidates
- slow/fast agreement vs disagreement expectancy

### 18.4 Primary Hypothesis to Test

> The context layer improves expectancy primarily by rejecting low-quality fast trades, not by substantially increasing raw directional hit rate.

---

## 19. Validation Standard

Validation must be stricter than ordinary backtest convenience.

### 19.1 Required Standards

- compare base fast model vs fast plus context directly
- use purged walk-forward validation, not random splits
- keep train/test boundaries realistic in time
- include transaction costs, slippage, and conservative execution assumptions
- measure whether improvements come from selection, sizing, or both

### 19.2 Required Ablations

Run these ablations separately:

1. 1h features only
2. 4h features only
3. 1h + 4h features
4. volume features only
5. volatility features only
6. gating only
7. gating + dynamic thresholds
8. gating + dynamic thresholds + size changes

### 19.3 Failure Criteria

Reject or delay rollout if:

- the context layer improves backtest but not paper expectancy
- gains come only from implausibly low-trade subsets
- regime definitions are unstable across adjacent windows
- live shadow results diverge sharply from paper behavior

---

## 20. Rollout Plan

### Phase 0: Instrumentation Only

- implement rolling context state
- compute features live
- log context snapshot
- do not affect trading behavior

### Phase 1: Paper Gating Only

- add allow/skip policy only
- no threshold or size changes yet
- compare accepted vs rejected trades

### Phase 2: Paper Dynamic Policy

- add threshold changes
- add confirm changes
- add max price changes
- add size multipliers

### Phase 3: Live Shadow

- compute context decisions in production
- log what would have happened
- do not alter live execution yet

### Phase 4: Limited Live Promotion

- activate only the lowest-risk policy adjustments first
- suggested order: size control, then thresholding, then hard skips

### Phase 5: Score Fusion Experiments

- only after gating and policy prove additive
- test context adjustment to fast alpha score

---

## 21. Expected Outcomes

### 21.1 Realistic Best Case

- fewer trades
- better average trade quality
- lower drawdown
- improved net expectancy
- more stable live behavior

### 21.2 Unrealistic Expectation to Avoid

- dramatically higher raw directional accuracy from 4-hour context alone

### 21.3 Most Likely Source of Incremental Edge

- avoiding bad regimes
- tightening thresholds in weak context
- reducing size when disagreement is high
- staying aggressive when fast and slow structure align

---

## 22. Risks and Failure Modes

| Risk | Description | Mitigation |
|---|---|---|
| Overfitting | too many horizons and thresholds memorize the past | start with 1h and 4h only |
| Redundant features | multiple slow features say the same thing | keep feature set small and ablate aggressively |
| Regime instability | classifier flips too often | use simple features and smoothing rules |
| Hidden complexity | policy interactions become hard to reason about | log every decision and keep mappings explicit |
| Live drift | paper improvement does not survive real execution | require live shadow before activation |
| Context latency | higher-timeframe state updates lag or desync | build deterministic bar aggregation and health checks |
| Signal starvation | context becomes too restrictive | monitor trade count and accepted-candidate ratio |

---

## 23. Recommendation

Build this, but build it conservatively.

The right first version is **not** a grand multi-horizon prediction engine.

The right first version is:

- a small, explicit higher-timeframe context layer
- 1h and 4h features first
- regime classification first
- gating and sizing first
- strong paper ablations first

This approach gives the strategy a realistic chance to extract additional edge while preserving the main thing that currently works: the fast live signal logic.

---

## 24. Concrete v1 Build Spec

If implementation starts immediately, v1 should include exactly this:

### 24.1 Data

- rolling 1-minute BTC bars with at least 24h retention
- rolling 5-minute BTC bars with at least 7d retention

### 24.2 Features

- 15m return
- 1h return
- 4h return
- 30m realized vol
- 4h realized vol
- 1h path efficiency
- 1h trend slope
- distance to 1h VWAP
- distance to 4h VWAP
- distance to daily open
- 4h range position
- volume percentile by hour-of-week

### 24.3 Regimes

- trend and liquid
- trend but overstretched
- chop and high vol
- quiet and illiquid
- neutral mixed

### 24.4 Policy Controls

- allow or deny trade
- min confidence adjustment
- confirm multiplier
- max entry price adjustment
- size multiplier

### 24.5 Evaluation

- base fast vs gated fast vs gated-plus-policy
- paper trading first
- purged walk-forward second
- live shadow third

---

## 25. Open Questions

These questions should be resolved during implementation planning:

1. Should context state live inside `signal-engine` or in a dedicated sidecar service?
2. Should volume percentile use raw BTC volume, trade count, or both?
3. Should policy affect only entry eligibility first, or also candidate ranking inside the market window?
4. Should slow-fast disagreement reduce size only, or also tighten price cap immediately?
5. What minimum paper sample size is required before promoting any policy change live?

---

## 26. Final Decision

**Recommendation:** Proceed.

The likely edge is real, but it should be pursued in the correct order:

1. treat higher-timeframe context as a conditioning layer
2. start with 1h and 4h context only
3. use it first for gating and sizing
4. keep the model interpretable
5. demand strong paper and walk-forward evidence before live promotion

This is the highest-probability path to extracting additional edge without turning a strong short-horizon system into an overfit macro experiment.