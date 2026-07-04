# TDR: Paper-Trade Tournament Dashboard (Clone of `/redeem/`)

**Date:** 2026-03-14
**Status:** Proposed
**Author:** Greg
**Depends On:** [Paper-Trade Multi-Strategy Runner TDR](paper_trade_multi_strategy_tdr.md)

---

## 1. Problem Statement

The paper-trade runner (previous TDR) will produce trade logs from up to 10 strategy variants running simultaneously. We need a dedicated dashboard to visualize and compare them.

**Goal:** Clone the live redeem dashboard (`/redeem/`, port 8006) to a **completely separate** paper-trade endpoint (`/paper/`, port 8007). The live dashboard is untouched — zero changes to anything under `services/redeem-positions/`. The paper clone is then optimized specifically for multi-strategy comparison.

---

## 2. Isolation Guarantee

```
LIVE (untouched)                    PAPER (new clone)
────────────────                    ─────────────────
Port 8006                           Port 8007
/redeem/                            /paper/
dashboard.html                      tournament.html
redeem_positions_service.py         paper_dashboard_service.py
Reads: Polymarket chain + API       Reads: local CSV files only
Writes: on-chain redemptions        Writes: nothing (read-only)
Source of truth: real money          Source of truth: simulated P&L
```

**What gets cloned:** `dashboard.html` → `tournament.html` (copied, then modified)
**What does NOT get cloned:** `redeem_positions_service.py` (paper backend is written from scratch — different data source)

**Directory structure:**
```
services/
  redeem-positions/           ← UNTOUCHED, live dashboard
    dashboard.html
    redeem_positions_service.py
  paper-tournament/           ← NEW, paper-only
    tournament.html           ← cloned + optimized from dashboard.html
    paper_dashboard_service.py
    sample_data/              ← test CSVs for local dev
```

---

## 3. Source Dashboard: `/redeem/`

**File:** `services/redeem-positions/dashboard.html` (417 lines, self-contained)
**Served by:** Python HTTP server on port 8006
**Tech:** Vanilla HTML + CSS + JS, no framework. Dark theme (#0a0a1a), Inter font, canvas charting.

### What We Inherit

| Feature | Implementation |
|---------|---------------|
| **Version filter buttons** | Dynamic buttons from `signal_version` field — "All (N)" + per-version |
| **Stats grid** (6 cards) | Total P&L, Win Rate, Streak, Avg Win, Avg Loss, Profit Factor |
| **Equity curve** | Canvas-drawn line chart with trade dots (green/red), starting balance ref line |
| **Trade log table** (13 cols) | #, Market, Time, Side, Size, Avg Price, Cost, Payout, P&L, P&L%, Result, Version, Tx |
| **Version badges** | Color-coded per version |
| **Auto-refresh** | Polls every 120s |

### What We Change in the Clone

| Redeem (live) | Paper (clone) | Why |
|---------------|---------------|-----|
| Filters by `signal_version` | Filters by `strategy` name | Up to 10 named strategies, not version tags |
| Single equity curve | **Overlaid multi-strategy equity curves** on All-view | Core comparison feature |
| No comparison table | **Strategy leaderboard table** on All-view | See all strategies ranked at a glance |
| Tx column (PolygonScan link) | **Strategy badge** column | No on-chain transactions in paper mode |
| 13 trade log columns | **Swap 3 columns** — drop Tx/Payout/Cost, add Strategy/Confidence/Edge | Paper-relevant signal data |
| 6 stat cards | **8 stat cards** — add Trades/Day + Brier Score | Key paper-trade metrics |
| Polls `/redeem/ledger` | Polls `/paper/ledger` + `/paper/compare` | Different data source |
| 120s refresh | **30s refresh** | Paper trades resolve fast, want near-real-time feedback |

---

## 4. Optimized Paper-Trade Views

### 4.1 "All Strategies" View (default landing)

When the filter is set to "All", the dashboard shows a **tournament-style comparison**:

```
┌──────────────────────────────────────────────────────────────────┐
│  Paper-Trade Tournament                              ↻ Refresh  │
│  10 strategies · 3,452 trades · Running 48h                     │
├──────────────────────────────────────────────────────────────────┤
│  [● All] [v14_baseline] [v14.1_no_volgate] [v15_brier_cb] ...  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  STRATEGY LEADERBOARD (sortable by clicking column headers)      │
│  ┌───┬──────────────────┬───────┬──────┬────────┬──────┬──────┐ │
│  │ # │ Strategy         │Trades │ WR%  │ PnL    │ T/Day│ MDD% │ │
│  ├───┼──────────────────┼───────┼──────┼────────┼──────┼──────┤ │
│  │ 1 │ ● v14.1_no_volg  │  312  │72.1% │ +$198  │ 56.1 │ 4.2% │ │
│  │ 2 │ ● v15_brier_cb   │  201  │74.8% │ +$145  │ 36.2 │ 3.8% │ │
│  │ 3 │ ● v14_baseline   │  146  │76.0% │ +$82   │ 26.3 │ 5.1% │ │
│  │ 4 │ ● v14_relaxed    │   89  │71.9% │ +$31   │ 16.0 │ 7.3% │ │
│  │   │ (gray = <30 trades, stats unreliable)                │ │
│  └───┴──────────────────┴───────┴──────┴────────┴──────┴──────┘ │
│                                                                  │
│  OVERLAID EQUITY CURVES (click legend to toggle lines)           │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │  $200 ─                          ╱─── v14.1 (blue)     │     │
│  │       ─                    ╱────╱                       │     │
│  │  $150 ─               ╱──╱     ╱──── v15 (green)       │     │
│  │       ─          ╱───╱──╱─────╱                         │     │
│  │  $100 ─ ────────╱──╱──╱──────────── v14 (orange)       │     │
│  │       ─                                                 │     │
│  │  $80  ─ · · · · · · · · · · starting balance · · · · · │     │
│  │                                                         │     │
│  │  x-axis: time (hours elapsed)                           │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                  │
│  ALL TRADES (merged, most recent first)                          │
│  # │ Strategy │ Market │ Time │ Side │ Conf │ Edge │ P&L │ Res  │
│  1 │ ●v14.1   │ 12:15  │10:17 │ UP   │ 0.82 │ 0.17 │+$1.2│ WIN │
│  2 │ ●v15     │ 12:15  │10:22 │ UP   │ 0.79 │ 0.14 │+$0.9│ WIN │
│  3 │ ●v14     │ 12:00  │10:05 │ DOWN │ 0.71 │ 0.10 │-$0.8│LOSS │
└──────────────────────────────────────────────────────────────────┘
```

**Leaderboard columns:**

| Column | Description |
|--------|-------------|
| **#** | Rank by PnL (default sort) |
| **Strategy** | Name + color dot |
| **Trades** | Total count (grayed out if <30 = unreliable) |
| **WR%** | Win rate, green ≥70%, yellow 55-70%, red <55% |
| **PnL** | Cumulative paper P&L |
| **T/Day** | Trades per day — the frequency metric we're optimizing |
| **MDD%** | Max drawdown percentage (peak-to-trough) |
| **Brier** | Rolling Brier score (only for v15 strategies, "-" for others) |
| **PF** | Profit factor (total wins / total losses) |

### 4.2 Single-Strategy View (click any strategy button)

Identical structure to the live redeem dashboard, but with paper-optimized stat cards:

```
┌──────────────────────────────────────────────────────────────────┐
│  [All] [v14_baseline] [● v14.1_no_volgate] [v15_brier_cb] ...  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  STATS GRID (8 cards, 4×2)                                      │
│  ┌──────────────┬──────────────┬──────────────┬──────────────┐  │
│  │  Total P&L   │  Win Rate    │  Trades/Day  │  Brier Score │  │
│  │  +$198.34    │  72.1%       │  56.1        │  0.183       │  │
│  │  ROI: 248%   │  225W / 87L  │  312 total   │  CB: 14 skip │  │
│  ├──────────────┼──────────────┼──────────────┼──────────────┤  │
│  │  Streak      │  Avg Win     │  Avg Loss    │  Profit Fac  │  │
│  │  4W          │  +$1.24      │  -$0.92      │  3.04×       │  │
│  └──────────────┴──────────────┴──────────────┴──────────────┘  │
│                                                                  │
│  EQUITY CURVE (single strategy, same style as live redeem)       │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │  [same canvas chart as current redeem dashboard]         │     │
│  │  green dots = wins, red dots = losses                    │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                  │
│  TRADE LOG (filtered to this strategy only)                      │
│  # │ Market │ Time │ Side │ Confidence │ Edge │ Regime │ P&L    │
└──────────────────────────────────────────────────────────────────┘
```

**Two new stat cards over live redeem:**
- **Trades/Day** — the core metric we're optimizing for. `total_trades / hours_running * 24`
- **Brier Score** — rolling recalibrated Brier + circuit breaker skip count (v15 only, shows "-" for v14 variants)

### 4.3 Paper-Specific Optimizations

Things we change from the live redeem clone because paper trading has different needs:

| Optimization | Rationale |
|--------------|-----------|
| **30s refresh** (vs 120s) | Paper markets resolve every 15 min, want near-real-time during tournament |
| **Time-normalized x-axis** | Equity curve x-axis uses wall-clock time (hours elapsed), not trade number — so strategies with different frequencies are visually comparable |
| **"Insufficient data" indicator** | Strategies with <30 trades get a gray warning badge — WR is unreliable |
| **Regime column** in trade log | Redeem dashboard doesn't show this — but for paper trading, comparing how strategies perform in Trend vs Neutral is key |
| **Drop cost/payout/tx columns** | Paper trades don't have real execution details — replace with signal-quality columns (Confidence, Edge, Regime) |
| **Sortable leaderboard** | Click any column header to re-sort — quickly answer "which strategy has best WR?" vs "which trades most?" |
| **Toggleable equity lines** | Click legend entry to show/hide that strategy's curve — essential when 10 lines overlap |

### 4.4 Strategy Color Scheme

10 distinct, high-contrast colors on dark background:

```
v14_baseline      → #ffa726 (orange)
v14.1_no_volgate  → #42a5f5 (blue)
v15_brier_cb      → #66bb6a (green)
v14_relaxed       → #ab47bc (purple)
v14_wide_confirm  → #ef5350 (red)
v14_tight_regime  → #26c6da (cyan)
v15_aggressive    → #ffd93d (yellow)
v14_low_whipsaw   → #ec407a (pink)
custom_1          → #8d6e63 (brown)
custom_2          → #78909c (slate)
```

Applied consistently to: filter buttons, leaderboard dots, equity curves, trade log badges.

---

## 5. API Endpoints (port 8007)

Completely separate from the live redeem service (port 8006).

**`GET /paper/health`**
```json
{
  "status": "ok",
  "strategies": 10,
  "total_trades": 3452,
  "running_since": "2026-03-14T00:00:00Z",
  "hours_elapsed": 48.2,
  "csv_dir": "<DATA_VOLUME>/paper_logs/"
}
```

**`GET /paper/ledger`** — All trades merged from all strategy CSVs:
```json
[
  {
    "strategy": "v14_baseline",
    "timestamp": "2026-03-14T10:15:00Z",
    "slug": "btc-updown-15m-1710432000",
    "direction": "UP",
    "confidence": 0.72,
    "edge": 0.12,
    "regime": "Trend",
    "entry_ask": 0.505,
    "result": "WIN",
    "pnl": 0.88,
    "bankroll": 101.76,
    "brier_score": 0.18,
    "cb_paused": false
  }
]
```

**`GET /paper/compare`** — Pre-computed leaderboard stats:
```json
[
  {
    "strategy": "v14_baseline",
    "color": "#ffa726",
    "trades": 146,
    "wins": 111,
    "win_rate": 76.0,
    "total_pnl": 82.34,
    "roi_pct": 102.9,
    "trades_per_day": 26.3,
    "max_drawdown_pct": 5.1,
    "profit_factor": 3.17,
    "avg_win": 1.24,
    "avg_loss": -0.92,
    "brier_avg": null,
    "cb_pauses": 0
  }
]
```

**`GET /paper/`** — Serves `tournament.html`

### Paper Trade CSV Schema

Each paper executor writes one CSV:
```csv
timestamp,strategy,slug,direction,confidence,edge,regime,entry_ask,result,pnl,bankroll,brier_score,cb_paused
1710432000000,v14_baseline,btc-updown-15m-1710432000,UP,0.72,0.12,Trend,0.505,WIN,0.88,101.76,0.18,false
```

---

## 6. Implementation Plan

### 6.1 Clone Dashboard HTML

Copy `services/redeem-positions/dashboard.html` → `services/paper-tournament/tournament.html`, then modify:

| Section | Change | Delta |
|---------|--------|-------|
| Title/subtitle | "Paper-Trade Tournament" | 2 lines |
| CSS | Strategy color classes, leaderboard styles, toggle states | +40 lines |
| Data URLs | `/paper/health`, `/paper/ledger`, `/paper/compare` | 3 lines |
| Refresh interval | 30s instead of 120s | 1 line |
| Filter bar | Key on `strategy` field instead of `signal_version` | ~5 lines |
| **NEW: Leaderboard** | Sortable HTML table populated from `/paper/compare` | +50 lines JS |
| **NEW: Overlaid equity** | Multi-line canvas chart with toggleable legend | +70 lines JS |
| **NEW: View-mode toggle** | Show leaderboard+overlay on "All", single stats on filter | +20 lines JS |
| Stat cards | Add Trades/Day and Brier Score cards (8 total) | +15 lines |
| Trade log columns | Drop Tx/Cost/Payout, add Strategy/Confidence/Edge/Regime | ~15 lines |

**Estimated:** 417 base + ~220 delta = **~640 lines** total.

### 6.2 Backend Server (new file)

`services/paper-tournament/paper_dashboard_service.py`:

| Component | Description | Lines |
|-----------|-------------|-------|
| CSV reader | Glob `paper_log_*.csv`, parse, tag with strategy | ~40 |
| In-memory cache | Re-read CSVs on `mtime` change, not every request | ~20 |
| `/paper/health` | Strategy count, total trades, uptime | ~15 |
| `/paper/ledger` | Merged + time-sorted trades | ~10 |
| `/paper/compare` | Per-strategy WR, PnL, T/day, MDD, PF | ~60 |
| HTTP handler | Serve `tournament.html` at `/` + JSON APIs | ~30 |

**Estimated:** ~175 lines Python.

### 6.3 Task Breakdown

| Step | Description |
|------|-------------|
| 1 | `mkdir services/paper-tournament/` |
| 2 | `cp services/redeem-positions/dashboard.html services/paper-tournament/tournament.html` |
| 3 | Update title, subtitle, data URLs, refresh interval |
| 4 | Rewrite filter bar to use `strategy` field |
| 5 | Add leaderboard table (All-view) with sortable columns |
| 6 | Add overlaid multi-equity chart with toggleable legend (All-view) |
| 7 | Add view-mode toggle (leaderboard vs single-strategy drill-down) |
| 8 | Update stat cards (8 cards: +Trades/Day, +Brier) |
| 9 | Update trade log columns (drop Tx/Cost/Payout, add Strategy/Conf/Edge/Regime) |
| 10 | Apply strategy color scheme (dots, badges, curves) |
| 11 | Write `paper_dashboard_service.py` |
| 12 | Create sample CSVs in `sample_data/` for local testing |
| 13 | Test both views end-to-end |

**Total new code:** ~400 lines (225 HTML/JS + 175 Python)

---

## 7. Deployment

```bash
# Both services run side-by-side, completely independent:

# LIVE (existing, untouched):
python3 services/redeem-positions/redeem_positions_service.py  # port 8006

# PAPER (new):
python3 services/paper-tournament/paper_dashboard_service.py \
  --csv-dir <DATA_VOLUME>/paper_logs/ \
  --port 8007                                                   # port 8007
```

Nginx routes:
```nginx
# Existing — no changes
location /redeem/ {
    proxy_pass http://127.0.0.1:8006/;
}

# New
location /paper/ {
    proxy_pass http://127.0.0.1:8007/;
}
```

**Access:**
- Live dashboard: `http://vps-ip/redeem/` (unchanged)
- Paper dashboard: `http://vps-ip/paper/`

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| 10 overlaid equity curves are unreadable | Can't compare | Toggleable legend — click to show/hide each line |
| Canvas chart slow with 1000+ trades | Laggy refresh | Downsample to 1 point per 15 min on All-view, full detail on drill-down |
| CSV files grow large over weeks | Slow API responses | In-memory cache, re-read only on file mtime change |
| <30 trades gives unreliable WR | Misleading leaderboard | Gray badge + warning text: "insufficient data" |
| Accidentally modify live redeem dashboard | Breaks prod | Separate directory, separate service, separate port. Nothing in `redeem-positions/` is touched |

---

## 9. Future Extensions (Out of Scope)

- **Auto-promote:** When a paper strategy hits 100+ trades with >70% WR, prompt for live deployment
- **Live vs Paper split view:** Show live v14 next to its paper equivalent on same chart
- **Alerts:** Slack/Telegram when a paper strategy outperforms live
- **A/B test mode:** Randomly route markets to strategy A vs B for unbiased comparison

---

## 10. Decision

**Recommendation:** Build it. The clone is safe (live dashboard untouched), the paper-specific optimizations are targeted (leaderboard, overlaid curves, signal columns), and the backend is a thin ~175-line CSV-to-JSON server. The dashboard delta is ~225 lines on top of the 417-line base. Total effort: ~400 lines of new code.
