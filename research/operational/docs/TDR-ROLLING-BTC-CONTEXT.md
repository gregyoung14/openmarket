# TDR: Rolling Pre-Market BTC Context Buffer

**Status:** Proposed  
**Date:** 2026-03-19  
**Component:** `rust-services/signal-engine`

---

## Problem

### Current Behaviour

Every time a new Polymarket 15-minute market opens, `AppState::new_market()` calls:

```rust
self.trade_buffer.lock().clear();
*self.open_price.lock() = None;
```

All BTC trade history is wiped. The signal engine then spends the first **60 seconds** of every market doing nothing but accumulating enough trades to:

1. Build 60 bars of 1-second closes for `detect_regime()` (`REGIME_LOOKBACK = 60`)
2. Accumulate `MIN_TRADES_FOR_SIGNAL = 20` raw Binance trades
3. Calculate OFI acceleration (split-window, needs bars from both halves)

This 60-second warmup means **the signal engine cannot enter during the first 10% of every market window**.

### Real-World Impact (Observed 2026-03-19)

On market `btc-updown-15m-1773898200` the UP token was already priced at **0.61–0.66** by the time the first signal was evaluated at second 129. The move from ~0.50 to 0.61 happened in the first ~2 minutes — inside the dead zone. The signal engine correctly identified conf=1.0, strong trend, but could not enter because the price cap (0.55) rejected every tick for the rest of the window.

The engine was right. It just arrived too late to a party that started before it was allowed in the room.

### Root Cause

BTC trend is **continuous**. It doesn't restart at the Polymarket market boundary. The 60-second warmup is paying a knowledge tax that shouldn't exist — all that regime/OFI context was fully computed in the prior market and then thrown away.

---

## Proposed Solution

### Architecture

Add a **rolling pre-market buffer** that runs independently of Polymarket market boundaries. It continuously retains the last N seconds of Binance trades regardless of what the Polymarket feed is doing.

```
[Binance WS Feed]
       │
       ├──→ trade_buffer (current market, as today)
       │
       └──→ pre_market_buffer (rolling ring buffer, last 120s always)
                │
                └── on new_market(): seed trade_buffer with this history,
                    adjusting trade timestamps so scanner bar-building works
```

### Key Changes

#### 1. `config.rs`
```rust
/// Rolling pre-market history to carry across market boundaries (seconds)
pub const PRE_MARKET_WINDOW_SECS: u64 = 120;

/// Minimum seconds into market before scanning.
/// Lowered from 60 → 5 because pre-market history provides regime context.
pub const MIN_SECS_INTO_MARKET: u64 = 5;
```

#### 2. `state.rs` — Add `pre_market_buffer`
```rust
/// Always-on rolling ring of the last PRE_MARKET_WINDOW_SECS of BTC trades.
/// Never cleared on new_market(). Seeded into trade_buffer at market open.
pub pre_market_buffer: Arc<Mutex<VecDeque<BinanceTrade>>>,
```

`push_trade()` always appends to `pre_market_buffer` and evicts trades older than `now - PRE_MARKET_WINDOW_SECS * 1000ms`.

`new_market()` seeds `trade_buffer` with a **copy** of `pre_market_buffer` contents, with timestamps remapped relative to `market.start_ms` so `build_1s_bars()` in the scanner works without modification:

```rust
let pre_history: Vec<BinanceTrade> = self.pre_market_buffer.lock()
    .iter()
    .map(|t| BinanceTrade {
        trade_time_ms: t.trade_time_ms,   // Keep real timestamps
        ..t.clone()
    })
    .collect();

*self.trade_buffer.lock() = pre_history;
```

#### 3. `scanner.rs` — `build_1s_bars` bar origin

`build_1s_bars` currently indexes bars from `market.start_ms`. With pre-market trades having timestamps *before* `start_ms`, the index calculation:

```rust
let sec_idx = ((trade.trade_time_ms - start_ms) / 1000) as usize;
```

...would underflow if trades are earlier than `start_ms`. The fix: use a `bar_origin_ms` that is `start_ms - PRE_MARKET_WINDOW_SECS * 1000`, and offset the signal computation accordingly so drift/OFI calculations still treat `start_ms` as "second 0" for the binary outcome.

The scanner passes `secs_in` (time since market start) for drift projection. Pre-market bars extend the historical window without changing the forward projection anchor.

#### 4. `open_price` logic

Currently set from the first trade `>= start_ms`. This is unchanged — the open price is still the first BTC tick after the Polymarket market opens, which defines the UP/DOWN binary correctly.

---

## What This Enables

| Before | After |
|--------|-------|
| Dead zone: first 60s every market | Dead zone: first 5s (connection + 1 scan tick) |
| Regime classifier cold at second 0 | 120s of regime history available at second 0 |
| BTC trend context lost at each boundary | Continuous rolling context across all markets |
| Price cap prevents late entries | Entry possible at ~0.50 when market first opens |

---

## Risks & Mitigations

**Risk: Pre-market regime misleads a new candle**  
If BTC was trending strongly UP in the prior 2 minutes but the new candle is going DOWN, we could get a bad early signal. **Mitigation:** Confirmation window (15–50s) is unchanged — signal must persist for 15+ seconds before entry. A true reversal will flip the regime classifier to neutral/chop within that window.

**Risk: `build_1s_bars` bar array size**  
Pre-market trades extend the time window. The bar array needs to be sized for `PRE_MARKET_WINDOW_SECS + secs_in` not just `secs_in`. Easy fix but needs careful indexing.

**Risk: Volume estimator double-counting**  
Pre-market trades seeded in are from the prior market's trade flow. `new_market()` already records prior volume into the `VolumeMedianEstimator` before clearing — seeded pre-market trades should not be re-counted. The estimator call happens before `trade_buffer` is populated, so order of operations naturally handles this.

---

## Files Affected

| File | Change |
|------|--------|
| `src/config.rs` | Add `PRE_MARKET_WINDOW_SECS`, lower `MIN_SECS_INTO_MARKET` to 5 |
| `src/state.rs` | Add `pre_market_buffer: Arc<Mutex<VecDeque<BinanceTrade>>>`, update `push_trade()`, update `new_market()` |
| `src/scanner.rs` | Update `build_1s_bars` to accept `bar_origin_ms` offset, resize bar array |
| `src/main.rs` | Initialize `pre_market_buffer` in `AppState::new()` |

No changes to `drift.rs`, `handlers.rs`, `upstream.rs`, or the paper-executor.

---

## Open Questions Before Implementation

1. **Window size:** 120s covers one full `REGIME_LOOKBACK` (60 bars) with headroom. Is that enough or do we want 300s (the prior full market's entry window)?
2. **Bar origin in drift:** Does `compute_drift_signal_v14` care about the absolute bar index, or only relative log returns? (Check `drift.rs:remaining_secs` projection math before assuming it's transparent.)
3. **Tournament experiment:** Should we add `MIN_SECS_OVERRIDE` env var to the tournament so we can A/B test an early-entry variant vs the baseline without changing all 6 strategies?
