# Execution Engine — Removed Internal "Fallback" Signal Strategy

## Overview

The original Execution Engine **v0.1.0** (commit `c2a9f9a`, Feb 14 2026) contained its own **internal signal aggregation and trade-decision logic**. Rather than waiting for an explicit `Entry` signal from the signal engine, the execution engine collected raw `Prediction` messages, aggregated them in a sliding window, and made its own rudimentary entry decisions based on hard-coded confidence/consistency thresholds and market timing gates.

This was **completely removed** in the current v1.0.0, which now states:

> *"100% signal-engine driven — No internal signal aggregation — signal engine is source of truth"*

Below is a step-by-step breakdown of how the removed fallback strategy worked.

---

## Architecture Comparison

| Aspect | v0.1.0 (Removed) | v1.0.0 (Current) |
|---|---|---|
| Trade trigger | Internal `SignalAggregator` consuming raw `Prediction` messages | Explicit `Entry` signal from signal engine |
| Decision-making | Execution engine applies its own thresholds | Signal engine applies all v9.2 filters |
| `strategy.rs` module | Present — `SignalAggregator` struct | **Deleted entirely** |
| `signal_aggregator` in state | Present in `AppState` | **Removed** |
| Config thresholds | `MIN_CONFIDENCE`, `MIN_CONSISTENCY`, `SIGNAL_WINDOW`, `MIN_PREDICTIONS`, timing gates | All removed — "no independent signal thresholds" |
| Paper bankroll | Internal `bankroll` & `peak_bankroll` tracking | Removed — on-chain USDC.e is sole source of truth |

---

## Step 1: Raw Predictions Arrive via WebSocket

The signal engine sent a stream of raw `Prediction` messages (not `Entry` signals) over WebSocket. Each prediction contained a direction, confidence, raw probability, and timing info:

```rust
// From models.rs (original v0.1.0)
#[derive(Debug, Clone, Deserialize)]
pub struct Prediction {
    pub direction: String,
    pub confidence: f64,
    pub raw_prob: f64,
    pub timestamp: Option<i64>,
    pub market: Option<String>,
    pub secs_in: Option<i64>,
    pub secs_left: Option<i64>,
    pub n: Option<u64>,
}
```

The execution engine *consumed* these in its `signal_processing_loop`. The key handler was `SignalMessage::Prediction(pred)`:

```rust
// From main.rs (original v0.1.0) — signal_processing_loop
SignalMessage::Prediction(pred) => {
    // Add to signal aggregator
    state.signal_aggregator.lock().add_prediction(&pred);

    // Check if we should enter
    let maybe_entry = {
        let agg = state.signal_aggregator.lock();
        let signal = agg.get_signal();
        let pm = state.position_manager.lock();
        let market = state.market_context.lock();

        match (signal, market.as_ref()) {
            (Some(signal), Some(market)) if pm.can_trade() => {
                // ... (entry decision logic)
            }
            _ => None,
        }
    };

    if let Some((signal, market, entry_ask)) = maybe_entry {
        execute_entry(Arc::clone(&state), &signal, &market, entry_ask).await;
    }
}
```

**Key point**: Every prediction triggered a re-evaluation of whether to enter. The execution engine didn't wait for the signal engine to tell it *when* to trade — it decided on its own.

---

## Step 2: Signal Aggregator — Sliding Window of Predictions

The `SignalAggregator` (in `strategy.rs`) collected the last N predictions in a `VecDeque` sliding window. This entire module was later deleted:

```rust
// From strategy.rs (original v0.1.0) — ENTIRE FILE WAS REMOVED

pub struct SignalAggregator {
    predictions: VecDeque<StoredPrediction>,
    current_market: Option<String>,
}

struct StoredPrediction {
    direction: Direction,
    raw_prob: f64,
}
```

### Adding Predictions

When a prediction arrived, it was pushed into the window. If the market slug changed, the buffer was **cleared** (reset):

```rust
pub fn add_prediction(&mut self, prediction: &Prediction) {
    // Reset on market change
    if let Some(ref market) = prediction.market {
        if self.current_market.as_ref() != Some(market) {
            info!(
                old = ?self.current_market,
                new = %market,
                "Market changed, resetting signal aggregator"
            );
            self.predictions.clear();
            self.current_market = Some(market.clone());
        }
    }

    let direction = Direction::from_str_loose(&prediction.direction)
        .unwrap_or(Direction::Up);

    self.predictions.push_back(StoredPrediction {
        direction,
        raw_prob: prediction.raw_prob,
    });

    // Keep only the last SIGNAL_WINDOW predictions
    while self.predictions.len() > config::SIGNAL_WINDOW {
        self.predictions.pop_front();
    }
}
```

Config constants controlling the window:

```rust
// From config.rs (original v0.1.0)
pub const SIGNAL_WINDOW: usize = 30;     // average last 30 predictions
pub const MIN_PREDICTIONS: usize = 5;    // need at least 5 before acting
```

---

## Step 3: Signal Aggregation — Simple Averaging

The `get_signal()` method computed an `AggregatedSignal` from the sliding window. The logic was intentionally rudimentary:

1. **Average the raw probabilities** across all predictions in the window
2. **Determine direction**: if avg > 0.5 → UP, else → DOWN
3. **Confidence** = the distance from 0.5 (i.e., `avg_raw_prob` for UP, `1.0 - avg_raw_prob` for DOWN)
4. **Consistency** = fraction of predictions agreeing with the dominant direction

```rust
pub fn get_signal(&self) -> Option<AggregatedSignal> {
    if self.predictions.len() < config::MIN_PREDICTIONS {
        return None;
    }

    let n = self.predictions.len();

    // Average raw probability
    let avg_raw_prob: f64 = self.predictions.iter()
        .map(|p| p.raw_prob)
        .sum::<f64>() / n as f64;

    // Direction and confidence from averaged probability
    let (direction, confidence) = if avg_raw_prob > 0.5 {
        (Direction::Up, avg_raw_prob)
    } else {
        (Direction::Down, 1.0 - avg_raw_prob)
    };

    // Consistency: fraction agreeing with dominant direction
    let up_count = self.predictions.iter()
        .filter(|p| p.direction == Direction::Up)
        .count();
    let down_count = n - up_count;
    let dominant_count = up_count.max(down_count);
    let consistency = dominant_count as f64 / n as f64;

    Some(AggregatedSignal {
        direction,
        confidence,
        consistency,
        n_predictions: n,
        avg_raw_prob,
    })
}
```

The resulting `AggregatedSignal`:

```rust
pub struct AggregatedSignal {
    pub direction: Direction,
    pub confidence: f64,
    pub consistency: f64,
    pub n_predictions: usize,
    pub avg_raw_prob: f64,
}
```

---

## Step 4: Hard-Coded Entry Gate — Confidence + Consistency + Timing

Back in `signal_processing_loop`, the aggregated signal was checked against multiple hard-coded thresholds. **All of these are now removed** from the execution engine:

```rust
// From main.rs (original v0.1.0) — inside SignalMessage::Prediction handler
let action = if signal.confidence >= config::MIN_CONFIDENCE
    && signal.consistency >= config::MIN_CONSISTENCY
    && secs_in >= config::MIN_SECS_INTO_MARKET
    && secs_in <= config::MAX_SECS_INTO_MARKET
{
    "ENTERING"
} else {
    "WATCHING"
};
```

The threshold constants:

```rust
// From config.rs (original v0.1.0)
pub const MIN_CONFIDENCE: f64 = 0.60;       // Need 60%+ confidence
pub const MIN_CONSISTENCY: f64 = 0.60;      // Need 60%+ of predictions agreeing
pub const MIN_SECS_INTO_MARKET: i64 = 30;   // Don't enter in first 30 seconds
pub const MAX_SECS_INTO_MARKET: i64 = 600;  // Don't enter after 10 minutes
```

**The rudimentary logic**: if the averaged probability of the last 30 predictions crossed 60% confidence with 60% consistency, and we're between 30 and 600 seconds into the market window → enter.

If the check passed (`action == "ENTERING"`), it grabbed the live ask price and returned the signal + market context for order execution:

```rust
if action == "ENTERING" {
    // Get entry ask from live prices
    let prices = state.live_prices.lock();
    let entry_ask = match signal.direction {
        Direction::Up => prices.up_ask.unwrap_or(0.5),
        Direction::Down => prices.down_ask.unwrap_or(0.5),
    };
    Some((signal, market.clone(), entry_ask))
} else {
    None
}
```

---

## Step 5: Paper Bankroll & Position Sizing

The original engine maintained an **internal paper bankroll** (disconnected from on-chain balance) with drawdown circuit breakers:

```rust
// From position.rs (original v0.1.0)
pub struct PositionManager {
    pub bankroll: f64,
    pub peak_bankroll: f64,
    pub positions: Vec<Position>,
    pub closed_positions: Vec<Position>,
    pub strategy: ExitStrategy,
}
```

Trade gating used the paper bankroll:

```rust
pub fn can_trade(&self) -> bool {
    if self.positions.len() >= config::MAX_OPEN_POSITIONS {
        return false;
    }
    if self.bankroll <= 1.0 {
        warn!(bankroll = self.bankroll, "Bankroll too low to trade");
        return false;
    }
    let drawdown = if self.peak_bankroll > 0.0 {
        (self.peak_bankroll - self.bankroll) / self.peak_bankroll
    } else {
        0.0
    };
    if drawdown >= config::MAX_DAILY_LOSS_PCT {
        warn!(drawdown = drawdown, "Max drawdown reached, halting");
        return false;
    }
    true
}
```

Position sizing was based on the paper bankroll (1% bet fraction):

```rust
pub fn create_position(
    &self,
    signal: &AggregatedSignal,
    market: &MarketContext,
    entry_ask: f64,
) -> Option<Position> {
    let entry_price = config::ceil_decimals(
        entry_ask + config::SLIPPAGE,
        config::ORDER_PRICE_DECIMALS,
    ).min(config::MAX_ENTRY_PRICE);

    let bet_amount = self.bankroll * config::BET_FRACTION;  // 1% of paper bankroll
    let fee_entry = bet_amount * config::FEE_RATE;
    let capital = bet_amount - fee_entry;
    let raw_shares = capital / entry_price;
    let shares = if config::ORDER_SIZE_DECIMALS == 0 {
        raw_shares.floor()
    } else {
        config::truncate_decimals(raw_shares, config::ORDER_SIZE_DECIMALS)
    };

    // ... build Position struct
}
```

Config:

```rust
pub const BET_FRACTION: f64 = 0.01;          // 1% per trade
pub const MAX_DAILY_LOSS_PCT: f64 = 0.20;    // 20% drawdown halt
pub const INITIAL_BANKROLL: f64 = 0.0;       // Seeded from on-chain balance
```

---

## Step 6: Post-Entry — Clear Aggregator + Record Trade

After a successful instant-fill, the signal aggregator was cleared to prevent repeat entries on stale data:

```rust
// From main.rs (original v0.1.0) — inside execute_entry
if instant_fill {
    info!(order_id = %resp.order_id, "Entry filled instantly");

    let event = state.position_manager.lock().register_open(position);
    state.broadcast(event);

    state.signal_aggregator.lock().clear();  // ← Reset the window
    state.position_manager.lock().save_trade_log("live_trades.json");
    return;
}
```

The market refresh loop also cleared the aggregator on each 15-minute boundary:

```rust
// From main.rs (original v0.1.0) — market_refresh_loop
if stale_or_missing {
    // ...clear prices, context...
    state.signal_aggregator.lock().clear();  // ← Also cleared here

    warn!(
        expected_start,
        "Market refresh tick: cleared stale/missing market context"
    );
}
```

---

## Why It Was Rudimentary

1. **Simple averaging**: Just averaged `raw_prob` across a sliding window — no regime detection, no OFI, no whipsaw scoring, no volume gating
2. **Fixed thresholds**: Hard-coded 60% confidence and 60% consistency — no adaptive confirmation or sustained-signal requirement
3. **No edge filter**: No check that `confidence - entry_price >= min_edge`
4. **No price cap**: No maximum entry price to prevent buying expensive positions (only a `MAX_ENTRY_PRICE = 0.99` cap at order time)
5. **No hour blacklist**: Would trade during any hour, including traditionally volatile open/close hours
6. **Paper bankroll**: Tracked an internal bankroll that could drift from reality

---

## What Replaced It

The current v1.0.0 execution engine stripped out all decision-making. The signal engine now:

- Applies v9.2 regime gating (skip chop periods)
- Uses adaptive confirmation (15–50s sustained signal)
- Enforces a price cap (≤ 0.55 entry ask)
- Applies EV edge filter (≥ 0.08)
- Has hour blacklists
- Sends a fully-formed `Entry` signal only when all filters pass

The execution engine's `Prediction` handler now says it all:

```rust
// Current v1.0.0 — Prediction handler
SignalMessage::Prediction(pred) => {
    // Informational only — log for monitoring, no trade decisions
    // ...
    tracing::debug!(
        direction = %direction,
        confidence = confidence,
        market = %market,
        secs_in = secs_in,
        "Prediction received (informational only)"
    );
}
```

And the `Entry` handler explicitly documents the shift:

```rust
// Current v1.0.0 — Entry handler
// THIS IS THE ONLY PATH THAT TRIGGERS TRADES.
// The signal engine has already applied all v9.2 filters:
//   - Regime gating (skip chop)
//   - Adaptive confirmation (15–50s sustained)
//   - Price cap (≤ 0.55)
//   - EV edge filter (≥ 0.08)
//   - Hour blacklist
// We trust the signal engine completely and just execute.
```
