# TDR: Paper Tournament Profitability Path

**Status:** Proposed  
**Date:** 2026-04-05  
**Primary Components:** `rust-services/signal-engine`, `rust-services/paper-executor`, `services/paper-tournament/paper_dashboard_service.py`

---

## 1. Executive Summary

The current paper tournament does **not** contain a live-ready strategy.

The user-facing conclusion from the v2 leaderboard is directionally correct:

- `v14_canary_early_highcap` is the only strategy that is plausibly worth iterating on.
- Every other strategy is materially under the win rate required to overcome its payoff profile.
- Even the canary is still losing money, so the right decision is **promote the canary design, not promote the canary as-is**.

The most important finding is that the canary is not losing because it is too aggressive on high-priced contracts. It is losing because the current candidate-selection logic systematically over-favors **cheap, mechanically high-edge contracts** that live results show are the worst part of the distribution.

The clearest path to profitability is:

1. Keep the canary's early-entry and higher-price-cap idea.
2. Stop taking the cheap-contract / very-high-edge trades that are poisoning the sample.
3. Add side-specific gating because current `DOWN` trades materially outperform `UP` trades.
4. Replace raw `edge` ranking with a net-EV or calibrated score before considering any live deployment.

If one strategy family is going to survive, it should be a **canary-derived v16**, not a baseline-derived v16.

---

## 2. Problem Statement

The v2 tournament leaderboard shows:

- the canary has the best win rate,
- the best drawdown profile,
- and the best profit factor,

but it is still negative PnL.

That creates a practical decision problem:

1. Is the canary actually closer to profitability, or merely the least-bad loser?
2. Is the problem win rate, payout asymmetry, sizing, or a bug in paper accounting?
3. What exact changes should be made next to move the strategy family into profitable territory?

This TDR answers those questions using the live v2 dataset currently shown by the dashboard.

---

## 3. Strategy Purpose

`v14_canary_early_highcap` exists to test one specific thesis: the baseline signal engine is arriving too late and skipping the strongest trending opportunities because its entry window starts too late and its price cap is too low.

That thesis is visible in two places:

1. The tournament launcher defines the canary as:

```bash
v14_canary_early_highcap|8016|9016|MIN_SECS_OVERRIDE=15 MAX_ENTRY_PRICE_OVERRIDE=0.75
```

2. The earlier rolling-context TDR explains the motivating problem: a large portion of strong moves occurs before the baseline is even allowed to evaluate or before the market price stays below the standard `0.55` cap.

### Intended Role

The canary is therefore not just “more aggressive.” It is a targeted experiment for:

- entering earlier (`15s` instead of `60s`),
- allowing stronger-trend contracts up to `0.75`,
- testing whether missed early/high-priced entries are the main reason the baseline family underperforms.

### What the data says about that thesis

The thesis was **partly correct**:

- the canary substantially improved win rate versus the baseline family,
- it reduced drawdown,
- and it exposed a profitable pocket of higher-priced trades that the baseline would never take.

But the canary also revealed a second problem:

- the current score selection logic loves cheap contracts with very large computed `edge`,
- and those trades perform catastrophically.

So the canary proved that **the current price cap is too restrictive**, but it also proved that **the current edge heuristic is not trustworthy across the full price range**.

---

## 4. Data and Methodology

### Data sources used

- Live dashboard comparison endpoint: `GET /paper/compare?version=v2`
- Live paper logs in `/var/lib/polymarket/paper_logs/paper_log_*.csv`
- Tournament launcher: `services/paper-tournament/start_paper_tournament.sh`
- Signal filters and candidate selection:
  - `rust-services/signal-engine/src/config.rs`
  - `rust-services/signal-engine/src/scanner.rs`
  - `rust-services/signal-engine/src/state.rs`
- Paper sizing and settlement logic:
  - `rust-services/paper-executor/src/main.rs`

### Version selection

The dashboard currently splits v1 and v2 by the same timestamp-gap logic implemented in `paper_dashboard_service.py`. For this analysis, the v2 cutover timestamp was reproduced using the same approach:

- `cutover_ts = 1774816321185`

This matters because the screenshot and the user's comments refer to **v2**, not the combined all-time dataset.

### Trade reconstruction

For each `(strategy, slug)`:

1. Keep the final resolved `WIN` or `LOSS` row.
2. Use the corresponding `PENDING` row to recover the pre-trade bankroll when needed.
3. Recompute win PnL using the paper executor's current quarter-Kelly sizing formula and fee assumptions.

### Accounting result

For the v2 slice, reconstructed win PnL changed the displayed results by `0.00` to `0.01` dollars per strategy. That means the current v2 leaderboard is reliable enough for strategy decisions.

This is important because it means the canary is still negative even after checking the known paper-audit path.

---

## 5. v2 Strategy Summary

The table below combines the dashboard's current v2 results with the reconstructed break-even requirement from the realized payoff profile.

| Strategy | Trades | Win Rate | PnL | Profit Factor | Max DD | Break-Even WR | Gap to Break-Even |
|---|---:|---:|---:|---:|---:|---:|---:|
| `v14_canary_early_highcap` | 69 | 56.5% | -15.52 | 0.90 | 45.8% | 59.0% | -2.5 pts |
| `v14.1_no_volgate` | 140 | 47.1% | -44.17 | 0.88 | 106.7% | 50.3% | -3.2 pts |
| `v15_brier_cb` | 141 | 46.8% | -47.16 | 0.87 | 109.2% | 50.2% | -3.4 pts |
| `v14_tight_regime` | 47 | 44.7% | -29.33 | 0.78 | 58.4% | 50.8% | -6.1 pts |
| `v14_wide_confirm` | 47 | 44.7% | -29.53 | 0.78 | 58.3% | 50.9% | -6.2 pts |
| `v14_baseline` | 48 | 43.8% | -33.19 | 0.76 | 62.2% | 50.6% | -6.8 pts |
| `v14_relaxed_conf` | 50 | 42.0% | -38.98 | 0.73 | 68.0% | 49.9% | -7.9 pts |

### Interpretation

There are two separate statements here:

1. **Relative ranking:** the canary is clearly first. It has the best win rate, the best drawdown, the best PnL, and the best profit factor.
2. **Absolute viability:** the canary is still below the win rate it needs to be profitable.

That means the right conclusion is not “run canary live.” The right conclusion is “the next strategy generation should be built on canary assumptions.”

---

## 6. Canary Deep Dive

### 6.1 Headline numbers

For v2, the canary currently looks like this:

- trades: `69`
- wins: `39`
- win rate: `56.5%`
- PnL: `-$15.52`
- profit factor: `0.90`
- average win: `$3.68`
- average loss: `-$5.30`
- break-even win rate: `59.0%`

That last line is the key: the strategy is only about `2.5` percentage points short of break-even, which is much better than the rest of the field, but still not enough.

### 6.2 Price bucket analysis

| Canary Bucket | Trades | Win Rate | PnL | Profit Factor |
|---|---:|---:|---:|---:|
| `ask <= 0.35` | 10 | 10.0% | -33.47 | 0.33 |
| `0.35 < ask <= 0.55` | 24 | 50.0% | -3.72 | 0.94 |
| `0.55 < ask <= 0.65` | 22 | 68.2% | +9.81 | 1.27 |
| `ask > 0.65` | 13 | 84.6% | +11.86 | 2.03 |

### Interpretation

This is the single most important discovery in the entire review.

The common intuition would be:

- expensive contracts are dangerous,
- cheap contracts should be great if the model has an edge.

The live canary data says the opposite.

In the current signal family:

- **cheap contracts are the trap**, and
- **higher-priced canary entries are the profitable pocket**.

That means the strategy is not failing because it is willing to pay up to `0.75`. It is failing because it still takes the wrong low-price bets.

### 6.3 Side analysis

| Canary Side | Trades | Win Rate | PnL | Profit Factor |
|---|---:|---:|---:|---:|
| `DOWN` | 33 | 63.6% | +10.05 | 1.16 |
| `UP` | 36 | 50.0% | -25.57 | 0.73 |

### Interpretation

Current canary performance is not symmetric by side.

- `DOWN` is modestly profitable.
- `UP` is the main reason the overall strategy is still red.

This strongly argues for side-specific calibration or an explicit `DOWN`-favored variant in the next round.

### 6.4 Edge bucket analysis

| Canary Edge Bucket | Trades | Win Rate | PnL | Profit Factor |
|---|---:|---:|---:|---:|
| `edge < 0.20` | 36 | 77.8% | +41.43 | 1.96 |
| `0.10 <= edge < 0.20` | 23 | 78.3% | +31.04 | 2.17 |
| `edge >= 0.35` | 12 | 8.3% | -43.86 | 0.27 |

### Interpretation

This result is counterintuitive and extremely important:

- the current model does **best** in moderate-edge trades,
- and does **worst** in its supposedly strongest high-edge trades.

That means the current `edge` variable is not acting like a calibrated quality score. It is acting like a misleading selection feature in one part of the state space.

---

## 7. Why Canary Still Loses

The canary still loses for four concrete reasons.

### 7.1 Average loss is still larger than average win

Even with the best headline win rate in the tournament:

- average win: `$3.68`
- average loss: `-$5.30`

That creates a realized break-even requirement of roughly `59.0%` wins.

At `56.5%`, the strategy is close, but not there.

### 7.2 Cheap contracts are poisoning the sample

The canary loses `-$33.47` on only `10` cheap-contract trades (`ask <= 0.35`).

That bucket alone explains most of the strategy's total v2 loss.

If those trades were removed, the same v2 sample becomes positive.

### 7.3 UP-side performance is materially worse than DOWN-side performance

The canary is positive on `DOWN` and strongly negative on `UP`.

That means the model's directional skill is not currently symmetric. Treating both sides with the same entry thresholds is leaving money on the table and adding avoidable drawdown.

### 7.4 Candidate selection is rewarding the wrong thing

The current selection rule in `signal-engine/src/state.rs` chooses the best candidate by:

1. highest `edge`
2. then higher confidence
3. then lower price only as a tie-breaker

That logic sounds reasonable until you combine it with the current edge formula in `scanner.rs`:

```rust
edge = confidence - (entry_ask + slippage)
```

This means that, all else equal, **lower-priced contracts mechanically create larger `edge` values**.

So the strategy engine is doing this:

1. cheap contracts produce big `edge`
2. big `edge` wins the candidate race
3. big `edge` also increases Kelly size
4. the live results show that this exact region performs the worst

That is the root mismatch between the current logic and the observed trade outcomes.

---

## 8. All-Time Sanity Check

The v2 slice is the right decision dataset for the current dashboard, but it is still useful to check whether the canary pattern survives outside that narrow sample.

Across combined v1+v2 history, the same structure persists:

- the canary remains the least-bad strategy family,
- `ask > 0.55` remains profitable,
- `edge >= 0.35` remains the disaster bucket,
- and removing cheap contracts plus weak `UP` exposure remains positive.

That does **not** prove the filter is universal, but it does reduce the chance that the v2 result is just a small-sample accident.

---

## 9. Recommended Path Forward

### Decision

Do **not** promote any current tournament strategy live.

Instead:

1. keep the canary as the base design,
2. stop iterating on the baseline family as the primary path,
3. build a tighter canary-derived v16 aimed at removing the empirically bad regions.

### 9.1 Immediate tournament variants to add

The next tournament should add three new variants.

#### Variant A: `v16_canary_priceband`

Purpose: keep the canary's early/high-cap strength but remove the cheap-contract trap.

Recommended config direction:

- `MIN_SECS_OVERRIDE=15`
- `MAX_ENTRY_PRICE_OVERRIDE=0.75`
- new `MIN_ENTRY_PRICE_OVERRIDE=0.35` or `0.40`
- keep existing confidence threshold initially

Why this is justified:

- the v2 canary becomes positive (`+$17.95`) if trades with `ask <= 0.35` are removed.

#### Variant B: `v16_canary_down_bias`

Purpose: test whether current profitability is concentrated in `DOWN` and stop paying for weak `UP` predictions.

Recommended config direction:

- all of Variant A
- `DOWN` only, or at minimum much stricter `UP` entry rules

Why this is justified:

- v2 `DOWN` canary is already positive (`+$10.05`)
- v2 `UP` canary is the dominant loss source (`-$25.57`)

#### Variant C: `v16_canary_scorefix`

Purpose: stop ranking candidates by raw edge and use a score aligned with profit.

Recommended logic direction:

- rank by net expected value after fees/slippage, not raw `edge`
- or at minimum penalize cheap/high-edge candidates until calibration is fixed

Why this is justified:

- `edge >= 0.35` is the worst live bucket,
- while `edge < 0.20` is the profitable bucket.

### 9.2 If forced to pick one immediate candidate

If only one next-step strategy can be tested, it should be:

`v16_canary_down_bias`

with:

- early entry,
- high cap,
- cheap-contract suppression,
- and either `DOWN` only or substantially stricter `UP` rules.

This is the narrowest path from current data to a positive expectancy tournament candidate.

---

## 10. Required Code Changes

### `rust-services/signal-engine/src/config.rs`

Add tournament-safe env overrides for:

- `MIN_ENTRY_PRICE_OVERRIDE`
- `MAX_EDGE_OVERRIDE` or similar upper-edge guard
- optional side gating such as `ALLOW_UP` / `ALLOW_DOWN`

### `rust-services/signal-engine/src/scanner.rs`

Add:

- configurable minimum entry price,
- optional maximum edge gate,
- side-specific gating,
- and candidate scoring based on profitability rather than raw edge.

### `rust-services/signal-engine/src/state.rs`

Change `consider_best_candidate()` so the winner is not chosen by raw edge first.

The current logic is appropriate only if `edge` is calibrated across price buckets. The live canary data shows it is not.

### `services/paper-tournament/start_paper_tournament.sh`

Add the new v16 variants and keep the current canary as the benchmark control.

### `services/paper-tournament/paper_dashboard_service.py`

Optional but recommended:

- expose break-even win rate,
- expose gap-to-profitability,
- expose side split and price-bucket split for each strategy.

This will make future tournament decisions much less dependent on manual ad hoc analysis.

---

## 11. Better Objective Function

The current tournament effectively behaves as if larger raw edge means a better trade. The live results say that is false in the most important failure region.

The better objective is net expected value after fees and slippage.

For a binary contract with ask `a`, fee rate `f`, and calibrated win probability `p`:

$$
EV_{net} = p \cdot \frac{1 - f}{a} - (1 + f)
$$

This should be computed with a **calibrated** probability estimate, not the raw confidence number.

### Practical implication

The strategy should stop asking:

- “Which candidate has the highest raw edge?”

and start asking:

- “Which candidate has the highest calibrated net EV and acceptable drawdown impact?”

---

## 12. Sizing Recommendations

The current executor already uses quarter-Kelly with a `5%` bankroll cap. That sounds conservative, but Kelly is only safe when the probability estimate is well calibrated.

Right now it is not.

Recommended near-term changes:

1. Lower the tournament cap from `5%` to `2%` or `3%` for new experimental variants.
2. Add a drawdown throttle so sizing decays after a recent losing streak.
3. Consider extra size penalties in cheap-contract or low-confidence regions until calibration is proven.

The goal is not merely to improve Sharpe-like smoothness. The goal is to stop bad model regions from having enough weight to dominate total PnL.

---

## 13. Validation Plan

### Phase 1: Tournament-only

Run the new variants in paper only until they accumulate at least `100` resolved v2-style trades each.

Track:

- total PnL,
- profit factor,
- break-even win rate gap,
- max drawdown,
- side split,
- price-bucket split,
- edge-bucket split.

### Phase 2: Promotion gate

Do not consider live deployment unless a candidate clears all of the following:

1. positive corrected PnL,
2. profit factor `> 1.10`,
3. actual win rate at least `2` percentage points above realized break-even win rate,
4. max drawdown below `25%` to `30%`,
5. no single side or price bucket acting as a hidden catastrophic tail.

### Phase 3: Live shadowing

Before any real capital deployment:

1. mirror the chosen paper strategy alongside the execution engine,
2. compare fill assumptions and price drift between paper and live order book conditions,
3. verify that the profitable region survives real execution frictions.

---

## 14. Risks

### Risk: Overfitting to the current v2 slice

Mitigation:

- keep current canary as control,
- require at least `100` resolved trades for each new variant,
- and validate the same rules across combined history as a sanity check.

### Risk: `DOWN` edge is temporary regime luck

Mitigation:

- test a `DOWN`-biased variant and a price-band-only variant in parallel,
- do not assume side asymmetry is permanent until it survives new samples.

### Risk: Removing cheap contracts reduces opportunity too much

Mitigation:

- run both `MIN_ENTRY_PRICE=0.35` and `MIN_ENTRY_PRICE=0.40` variants,
- track trades/day and PF together.

### Risk: Candidate score fix changes behavior more than expected

Mitigation:

- deploy the score-fixed variant only as a parallel tournament branch first.

---

## 15. Final Recommendation

The right read is:

- **Yes**, `v14_canary_early_highcap` is the only current strategy family worth serious continuation.
- **No**, it is not yet good enough to trade as-is.

The canary proved two things at once:

1. earlier entries and a higher cap are directionally correct,
2. raw edge is selecting the wrong trades in the cheap-contract region.

So the next move is not “make the canary bigger.”

The next move is:

1. keep the canary's early/high-cap premise,
2. remove cheap-contract and high-edge trap trades,
3. split or heavily gate `UP` versus `DOWN`,
4. rank candidates by calibrated net EV,
5. only then re-evaluate for live readiness.

That is the clearest path from the current tournament to something that can plausibly become profitable.