# Execution Engine v15 — Specification

## Overview

v15 fixes two critical parsing bugs that caused **68% of entry signals to be rejected** ("Price mismatch")
and replaces flat 2% bet sizing with **half-Kelly criterion** position sizing.

---

## 1. Bug Fixes

### Bug A: Dead-Code Explicit Parser (`signal_consumer.rs`)

| Field | Before | After |
|-------|--------|-------|
| Trigger key | `value.get("event_type")` | `value.get("type")` |
| Location | `signal_consumer.rs:68` | `signal_consumer.rs:68` |

**Root cause:** `polymarket-websocket` broadcasts messages with `"type"` (e.g. `"type": "book"`),
but the execution engine's manual parser checked for `"event_type"` — a field that **never exists**
in the wire format. Every message fell through to the `serde::from_value` fallback, which:

- Kept `book` messages as `PolymarketMessage::Book` instead of converting them to a normalized
  `PolymarketMessage::PriceChange` with extracted best_bid/best_ask.
- Lost the explicit handling for order-book level parsing (supports both Object `{"0.44":"10"}` and
  Array `[[0.44, 10.0]]` formats).

**Impact after fix:** The explicit parser fires for both `"book"` and `"price_change"` messages.
Book messages are normalised into `PriceChange` with correctly extracted `best_bid` / `best_ask`,
and the `market_side` (UP/DOWN) is propagated to `apply_price_update`.

### Bug B: Book Side Resolution (`models.rs` + `main.rs`)

| Field | Before | After |
|-------|--------|-------|
| `PolymarketMessage::Book` fields | `side` only | `side` + `market_side` |
| `main.rs` Book handler | `side.as_deref()` | `market_side.as_deref().or(side.as_deref())` |

**Root cause:** `polymarket-websocket` uses inconsistent field naming:

| Message Type | Resolved UP/DOWN field | Raw order side field |
|-------------|----------------------|---------------------|
| `book` | `"side": "UP"` | *(none)* |
| `price_change` | `"market_side": "UP"` | `"side": "BUY"` |

The `Book` variant only had `side: Option<String>`, which happened to work for serde deserialization
(because `"side"` was present in the JSON). But if `polymarket-websocket` ever switches to
`"market_side"` for books (for consistency), the serde fallback path would silently lose the
resolved side.

**Fix:** Added `market_side: Option<String>` to the `Book` variant. The `main.rs` Book handler
now does `market_side.or(side)` — matching the `PriceChange` handler's behaviour.

**Note:** With Bug A fixed, all book messages are now handled by the explicit parser (converted to
PriceChange), so the serde fallback Book path is a safety net only.

### Combined Impact

Before these fixes, the execution engine's live price view was correct *most of the time* (the
serde fallback worked), but:

1. Book order-level parsing was less robust (serde path, not the explicit parser).
2. During market transitions, stale `market_context` could cause the asset_id fallback to fail,
   leaving price updates silently dropped.
3. 298 of 440 entry signals (68%) were rejected because of price drift between signal engine
   and execution engine price views.

---

## 2. Half-Kelly Criterion Sizing

### Formula

```
bet_fraction = KELLY_MULTIPLIER × edge × confidence
             = 0.5 × edge × confidence
```

Clamped to **[KELLY_MIN_FRACTION, KELLY_MAX_FRACTION]** = **[1%, 5%]** of wallet USDC.

### Constants (`config.rs`)

| Constant | Value | Purpose |
|----------|-------|---------|
| `KELLY_MULTIPLIER` | `0.5` | Half-Kelly (conservative) |
| `KELLY_MIN_FRACTION` | `0.01` | 1% floor — prevents dust trades |
| `KELLY_MAX_FRACTION` | `0.05` | 5% ceiling — limits max exposure |
| `BET_FRACTION` | `0.02` | Legacy (retained for reference only, unused) |

### Sizing Logic (`position.rs::create_position`)

```
edge         = signal.edge.unwrap_or(0.0).max(0.0)
kelly_raw    = 0.5 × edge × confidence
kelly_frac   = clamp(kelly_raw, 0.01, 0.05)
bet_amount   = wallet_usdc × kelly_frac
fee_entry    = bet_amount × FEE_RATE (1%)
capital      = bet_amount − fee_entry
shares       = floor(capital / entry_price)
```

### Example Scenarios

| Edge | Confidence | Kelly Raw | Clamped | Wallet $131 → Bet |
|------|-----------|-----------|---------|-------------------|
| 0.32 | 0.79 | 12.6% | **5.0%** | $6.55 |
| 0.14 | 0.66 | 4.6% | **4.6%** | $6.03 |
| 0.08 | 0.60 | 2.4% | **2.4%** | $3.14 |
| 0.05 | 0.55 | 1.4% | **1.4%** | $1.83 |
| 0.02 | 0.55 | 0.6% | **1.0%** | $1.31 |
| 0.00 | 0.70 | 0.0% | **1.0%** | $1.31 |

### Rationale

- **Half-Kelly** avoids the notorious overbetting problem of full Kelly.
- **1% floor** ensures we always place a meaningful trade (Polymarket minimum is ~$1).
- **5% ceiling** caps single-trade risk at 5% of bankroll.
- Edge and confidence both contribute — high-confidence/low-edge trades size smaller than
  high-confidence/high-edge trades.
- If `edge` is missing from the signal (shouldn't happen in v14), falls back to 1% floor.

### Test Mode Override

When `TEST_MODE=1`, Kelly sizing is bypassed. The engine buys exactly `TEST_MODE_SHARES` (1.0)
to meet Polymarket's $1 minimum order size. This is unchanged from v9.2.

---

## 3. Wire Format Reference

### polymarket-websocket → execution-engine (ws://127.0.0.1:8002/ws)

**Book message:**
```json
{
  "type": "book",
  "asset_id": "0xabc123...",
  "bids": [[0.44, 10.0], [0.43, 20.0]],
  "asks": [[0.46, 5.0], [0.47, 8.0]],
  "side": "UP",
  "timestamp": 1771696010648
}
```

**Price change message:**
```json
{
  "type": "price_change",
  "asset_id": "0xabc123...",
  "price": 0.46,
  "size": 5.0,
  "side": "BUY",
  "market_side": "UP",
  "best_bid": 0.45,
  "best_ask": 0.46,
  "timestamp": 1771696010648
}
```

**Key naming inconsistency (upstream):**
- Book messages: resolved side in `"side"` (UP/DOWN). No raw order side.
- Price changes: raw order side in `"side"` (BUY/SELL), resolved side in `"market_side"`.

The explicit parser in `signal_consumer.rs` handles both conventions.
The serde fallback path for the `Book` variant also handles both via `side` + `market_side` fields.

### signal-engine → execution-engine (ws://127.0.0.1:8003/ws)

**Entry signal:**
```json
{
  "type": "entry",
  "direction": "UP",
  "confidence": 0.79,
  "edge": 0.32,
  "entry_ask": 0.46,
  "entry_bid": 0.45,
  "market": "btc-updown-15m-1771695900",
  "secs_in": 110,
  "secs_left": 790,
  "regime": "trend",
  "adaptive_confirm": 38,
  "version": "v14"
}
```

---

## 4. Price Mismatch Guard

The execution engine maintains an independent price mismatch guard in `execute_signal_entry()`:

```
if |signal_engine_ask − live_feed_ask| > $0.10 → SKIP entry
```

This remains as a **safety net** against token-side mapping desync. With Bug A/B fixed, the
execution engine's live price view should closely track the signal engine's, reducing the mismatch
rate from ~68% to near 0%.

---

## 5. Data Flow Summary

```
Binance WS ─────→ signal-engine (8003) ─────→ execution-engine (8004)
                        ↑                            ↑
Polymarket CLOB WS → polymarket-ws (8002) ──────────┘
                        │
                        ├─ book msgs:         "type":"book",   "side":"UP"
                        └─ price_change msgs: "type":"price_change", "market_side":"UP"
```

execution-engine's explicit parser (`parse_polymarket_message`):
1. Reads `"type"` field (was broken: read `"event_type"` → dead code)
2. For `"book"`: extracts best_bid/best_ask from order levels, reads `"side"` + `"market_side"`
3. For `"price_change"`: reads price/side/market_side/best_bid/best_ask
4. Both normalized to `PolymarketMessage::PriceChange`
5. `apply_price_update()` resolves UP/DOWN via `market_side.or(side)` → asset_id fallback

---

## 6. Test Coverage

Unit tests verify:

### Parsing (`signal_consumer::tests`)
- Book with `"type":"book"` + `"side":"UP"` → correct best_bid/best_ask/market_side
- Book with `"type":"book"` + `"market_side":"UP"` → correct resolution
- Price change with `"type":"price_change"` + `"market_side":"DOWN"` → correct
- Book with real polymarket-websocket wire format (array-of-arrays bids/asks)
- Rejected invalid market_side values (e.g. "YES")

### Kelly Sizing (`position::tests`)
- High edge/confidence → capped at 5%
- Medium edge/confidence → correct fractional sizing
- Low edge → floored at 1%
- Zero/missing edge → floored at 1%
- Bet amount proportional to wallet USDC
- Share count correctly computed from (bet − fees) / entry_price

### Signal Parsing (`models::tests`)
- Entry signal v14 format round-trip
- Prediction, Connected, MarketInfo dispatch
- Unknown types → graceful fallthrough
- Direction parsing (case-insensitive)

---

## 7. Changelog

| Version | Date | Changes |
|---------|------|---------|
| v9.2 | — | Flat 2% BET_FRACTION, serde fallback parsing |
| **v15** | **2026-03-07** | Bug A fix (event_type→type), Bug B fix (Book market_side), half-Kelly sizing |
