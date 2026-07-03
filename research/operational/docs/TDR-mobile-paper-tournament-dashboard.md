# TDR: Mobile Paper Tournament Dashboard (Data Parity with Web)

**Status:** Proposed  
**Date:** 2026-03-30  
**Primary Component:** New mobile app client (iOS + Android)  
**Data Source:** `services/paper-tournament/paper_dashboard_service.py` APIs

---

## 1. Executive Summary

This TDR defines how to consume the exact same data points currently used by the web paper tournament dashboard and present them in a clean mobile dashboard with feature parity.

The mobile app will consume:
- `GET /paper/health?version=v1|v2|all`
- `GET /paper/ledger?version=v1|v2|all`
- `GET /paper/compare?version=v1|v2|all`

It will preserve current behavior:
- V1/V2 dataset toggle
- 30s refresh cadence
- all-strategy overview and single-strategy drilldown
- leaderboard metrics
- equity charts
- trade log including Buy Odds (`entry_ask`)

This document includes contract inventory, architecture, UI mapping, data modeling, performance strategy, rollout plan, and acceptance criteria.

---

## 2. Problem Statement

### Current State
The existing paper tournament dashboard is web-only. It provides strong visibility into strategy performance and trade outcomes but requires desktop/browser usage.

### Desired State
A mobile app should provide the same information with a clean, touch-first dashboard experience while preserving metric definitions and version split behavior.

### Why This Matters
- Fast on-the-go monitoring of paper strategy performance.
- Better accessibility for quick checks and incident response.
- Consistent analytics across web and mobile (same data, same formulas).

---

## 3. Goals and Non-Goals

### Goals
1. Reproduce all currently consumed web dashboard data points in mobile.
2. Preserve metric definitions and display semantics.
3. Keep API contract unchanged for initial release.
4. Deliver responsive performance for current and moderate growth data volumes.
5. Support V1/V2 toggle as first-class UX.

### Non-Goals (Phase 1)
1. No trading/execution actions in app (read-only monitoring).
2. No new backend endpoints required for v1 launch.
3. No authentication redesign in this phase (can layer later).
4. No push notifications in initial parity release.

---

## 4. Source of Truth and Existing Contracts

### 4.1 Endpoints

1. `GET /paper/health?version=<v1|v2|all>`
2. `GET /paper/ledger?version=<v1|v2|all>`
3. `GET /paper/compare?version=<v1|v2|all>`

Notes:
- `version` defaults by backend config if omitted.
- Web currently requests explicit `version` from toggle state.

### 4.2 Health Payload Inventory

| Field | Type | Produced By | Web Usage | Mobile Usage |
|---|---|---|---|---|
| `status` | string | backend | status dot/text | connection state indicator |
| `version` | string | backend | implied toggle context | active dataset badge |
| `strategies` | number | backend | subtitle, status, badges | overview header and KPI |
| `total_trades` | number | backend | subtitle, badges | top KPI card |
| `version_counts.v1` | number | backend | toggle labels | toggle labels |
| `version_counts.v2` | number | backend | toggle labels | toggle labels |
| `split_mode` | string | backend | split badge | data quality badge |
| `cutover_timestamp` | number/null | backend | not directly shown | debug details view |
| `cutover_iso` | string/null | backend | split badge date | split badge date |
| `running_since` | ISO string | backend | not shown | diagnostics info |
| `hours_elapsed` | number | backend | subtitle/badges/stats | uptime KPI, trade/day denominator fallback |
| `csv_dir` | string | backend | not shown | diagnostics-only (hidden behind dev toggle) |

### 4.3 Ledger Payload Inventory

Each item returned by `/paper/ledger`:

| Field | Type | Web Usage | Mobile Usage |
|---|---|---|---|
| `timestamp` | int | sort, chart X-axis, display time | same |
| `strategy` | string | filters, legends, table, grouping | same |
| `slug` | string | market label extraction | same |
| `direction` | string | table side | same |
| `confidence` | number | table and confidence interpretation | same |
| `edge` | number | table | same |
| `regime` | string | table | same |
| `entry_ask` | number | Buy Odds column | same (must preserve) |
| `result` | string | resolved filtering, win/loss styles | same |
| `pnl` | number | cards, leaderboard source, table | same |
| `bankroll` | number | charts and table | same |
| `original_pnl` | number | not displayed | diagnostics panel |
| `original_bankroll` | number | not displayed | diagnostics panel |
| `audit_entry_stake` | number/null | not displayed | diagnostics panel |
| `audit_pnl_delta` | number | not displayed | diagnostics panel |
| `is_audit_corrected` | bool | not displayed | audit marker chip in trade detail |
| `brier_score` | number/null | indirectly used in compare | optional trade detail |
| `cb_paused` | bool | compare aggregate | optional marker in trade row |
| `version` | string | split validation | local consistency checks, filter guard |

### 4.4 Compare Payload Inventory

Each item returned by `/paper/compare`:

| Field | Type | Web Usage | Mobile Usage |
|---|---|---|---|
| `strategy` | string | leaderboard row key | same |
| `color` | string | chart/legend colors | same |
| `trades` | int | leaderboard, insufficient marker | same |
| `wins` | int | not directly shown | strategy detail |
| `win_rate` | number | leaderboard, card thresholds | same |
| `total_pnl` | number | leaderboard sort and display | same |
| `roi_pct` | number | not shown in all-view table | strategy detail card |
| `trades_per_day` | number | leaderboard | same |
| `max_drawdown_pct` | number | leaderboard | same |
| `profit_factor` | number/null | leaderboard and card | same |
| `avg_win` | number | single-strategy cards (via recompute in web) | use backend directly for consistency |
| `avg_loss` | number | single-strategy cards (via recompute in web) | use backend directly for consistency |
| `brier_avg` | number/null | leaderboard + stats card | same |
| `cb_pauses` | int | stats card | same |

### 4.5 Derived Client-Side Metrics (Current Web Behavior)

These are computed client-side in web and should be preserved unless explicitly replaced with backend fields:

1. `resolvedTrades = trades where result in {WIN, LOSS}`
2. single-strategy streak (`nW` / `nL` from tail of resolved list)
3. fallback trades/day using `hours_elapsed`
4. market label parsing from slug (`slug.split("-").pop()`)
5. confidence, edge, odds formatting precision

Recommendation: keep these as shared pure functions in mobile to guarantee parity.

---

## 5. Mobile Product Requirements (Parity)

### 5.1 Functional Requirements

1. User can switch between V1 and V2 datasets.
2. App refreshes data every 30 seconds while foregrounded.
3. User can view all-strategy leaderboard and overlaid equity chart.
4. User can drill down to a single strategy with cards and single-line equity chart.
5. User can browse recent trades with Buy Odds column present.
6. User can see split metadata (`split_mode`, `cutover_iso`) when available.
7. Metrics match current web definitions.

### 5.2 UX Requirements

1. Clean layout for phone screens, no horizontal table dependency for core KPIs.
2. Readability in low light and daylight conditions.
3. Fast perception: primary KPIs visible in first viewport.
4. Minimal taps to reach strategy drilldown.

---

## 6. Proposed Mobile Information Architecture

### 6.1 Navigation

Bottom tabs:
1. Overview
2. Strategies
3. Trades
4. Health

Global controls in top app bar:
- Version segmented control: V1 / V2
- Refresh status and last sync timestamp

### 6.2 Screen Mapping to Existing Web

#### Overview Tab
- Subtitle equivalent: strategies, total trades, running hours.
- Badge row: version, split mode, cutover date.
- Overlaid equity chart (all visible strategies).
- Compact leaderboard top rows.

#### Strategies Tab
- Full sortable leaderboard list.
- Tap strategy -> Strategy Detail screen.

#### Strategy Detail Screen
- KPI cards: total pnl, win rate, trades/day, brier, streak, avg win/loss, PF, ROI.
- Single strategy equity chart.
- Recent trades for that strategy.

#### Trades Tab
- Virtualized trade list with filters:
  - strategy filter
  - result filter
  - time range quick chips (24h, 7d, all)
- Row fields: market, side, buy odds, conf, edge, pnl, bankroll, result.

#### Health Tab
- Raw contract visibility:
  - status, running_since, hours_elapsed
  - version counts
  - split diagnostics
- Optional debug panel for audit fields and contract checks.

---

## 7. Technical Architecture

### 7.1 Stack Decision

**Recommended:** React Native + Expo + TypeScript

Rationale:
1. Fast cross-platform delivery (iOS + Android).
2. Type-safe API contracts.
3. Strong ecosystem for charts, data fetching, and list virtualization.
4. Easy CI/CD with EAS.

### 7.2 Core Libraries

1. Networking/state:
   - `@tanstack/react-query`
   - `axios` or native `fetch`
2. Persistence:
   - `@react-native-async-storage/async-storage`
3. Charts:
   - `react-native-svg`
   - `victory-native` (or `react-native-gifted-charts`)
4. Lists:
   - `@shopify/flash-list`
5. Validation:
   - `zod` for runtime schema validation
6. Monitoring:
   - Sentry SDK (optional but recommended)

### 7.3 App Layering

1. `api/`
   - endpoint clients
   - request/response validators
2. `models/`
   - typed domain models
3. `selectors/`
   - derived metrics and parity functions
4. `screens/`
   - view composition
5. `components/`
   - reusable cards, badges, segmented controls, chart widgets
6. `store/`
   - UI preferences and ephemeral app state

---

## 8. Data Contract Models (TypeScript)

```ts
export type Version = "v1" | "v2" | "all";

export interface HealthResponse {
  status: string;
  version: Version;
  strategies: number;
  total_trades: number;
  version_counts: { v1: number; v2: number };
  split_mode: string;
  cutover_timestamp: number | null;
  cutover_iso: string | null;
  running_since: string;
  hours_elapsed: number;
  csv_dir: string;
}

export interface LedgerTrade {
  timestamp: number;
  strategy: string;
  slug: string;
  direction: string;
  confidence: number;
  edge: number;
  regime: string;
  entry_ask: number;
  result: "WIN" | "LOSS" | "PENDING" | string;
  pnl: number;
  bankroll: number;
  original_pnl: number;
  original_bankroll: number;
  audit_entry_stake: number | null;
  audit_pnl_delta: number;
  is_audit_corrected: boolean;
  brier_score: number | null;
  cb_paused: boolean;
  version: "v1" | "v2" | string;
}

export interface CompareRow {
  strategy: string;
  color: string;
  trades: number;
  wins: number;
  win_rate: number;
  total_pnl: number;
  roi_pct: number;
  trades_per_day: number;
  max_drawdown_pct: number;
  profit_factor: number | null;
  avg_win: number;
  avg_loss: number;
  brier_avg: number | null;
  cb_pauses: number;
}
```

---

## 9. Query and Sync Strategy

### 9.1 Polling

- Poll all 3 endpoints every 30s while app is foregrounded.
- Pause polling when app backgrounded.
- Force refresh on pull-to-refresh and on version toggle.

### 9.2 Cache Policy

- Keep latest successful snapshot in memory and AsyncStorage.
- Stale while revalidate approach:
  - show cached data instantly
  - annotate as stale if network request is pending or failed

### 9.3 Version Handling

- Global version state (`v1` or `v2`) in top app bar.
- Every request appends `?version=<selectedVersion>`.
- Health endpoint drives badge counts in segmented control labels.

---

## 10. Parity Computation Rules

These rules must be implemented as shared pure selectors.

### 10.1 Resolved Trades

```text
resolved = trade.result == WIN or trade.result == LOSS
```

### 10.2 Strategy Streak

- Traverse resolved trades in reverse chronological order.
- Count contiguous same result from latest trade.
- Output format: `<count>W` or `<count>L`.

### 10.3 Buy Odds Display

- Source field: `entry_ask`.
- Show `-` when null/0/non-finite.
- Precision: 3 decimals.

### 10.4 PnL Formatting

- Prefix plus sign for positive values.
- Green for positive/win, red for negative/loss.

### 10.5 Win Rate Color Buckets

- Green: `>= 70`
- Yellow: `>= 55 and < 70`
- Red: `< 55`

---

## 11. Mobile UI Component Specification

### 11.1 Top Bar

- Left: app title.
- Center: segmented control `V1 (count)` `V2 (count)`.
- Right: sync dot and last update age.

### 11.2 KPI Cards

- Total PnL
- Win Rate
- Trades/Day
- Brier Score
- Streak
- Avg Win
- Avg Loss
- Profit Factor

### 11.3 Leaderboard Row

Fields:
- rank
- strategy + color dot
- trades
- win rate
- pnl
- trades/day
- mdd
- brier
- pf

### 11.4 Trade Row

Fields:
- time
- strategy (optional on strategy detail)
- market label
- side
- buy odds
- confidence
- edge
- regime
- pnl
- bankroll (single-strategy view)
- result badge

---

## 12. Performance and Scalability

### 12.1 Current Observed Envelope

From live checks, current payloads are in low hundreds of trades. Design should still tolerate growth to several thousand rows.

### 12.2 Performance Controls

1. Use FlashList for trade and leaderboard lists.
2. Memoize derived selectors (`resolved`, grouped curves, streaks).
3. Avoid full re-sorts on every render; sort on dependency changes only.
4. Defer non-critical diagnostics rendering.

### 12.3 Future Backend Enhancements (Optional)

If payload growth becomes large, add:
1. `ledger?since=<ts>` incremental fetch.
2. `ledger?limit=<n>` server cap.
3. gzip/brotli transport on API gateway.
4. ETag / If-None-Match support.

---

## 13. Error Handling and Fallbacks

### 13.1 Network Failures

- Keep last good snapshot visible.
- Show stale banner with retry action.
- Avoid blank screen unless no cache exists.

### 13.2 Contract Drift

- Runtime schema validation with telemetry on mismatch.
- Fail soft for unknown fields.
- Fail visible for missing required fields used by core KPIs.

### 13.3 Split Metadata Absence

- If `cutover_iso` is null, hide split date chip.
- Keep version toggle functional from selected state + counts fallback.

---

## 14. Security and Privacy

### 14.1 Phase 1

- Read-only public/lan access like web dashboard.
- TLS required in production deployment path.

### 14.2 Recommended Hardening (Phase 2)

1. Add lightweight API key or JWT for mobile clients.
2. Add request throttling at edge.
3. Optional certificate pinning in mobile app.

No sensitive PII is expected in current payloads.

---

## 15. Observability

### 15.1 Client Telemetry

Capture:
1. endpoint latency per call
2. response size
3. schema validation failures
4. render time for heavy screens
5. app foreground refresh success rate

### 15.2 Health Signals

Expose in app Health tab:
- data age
- last successful sync
- endpoint status summary
- split mode and cutover

---

## 16. Test Strategy

### 16.1 Unit Tests

1. selector parity tests:
   - resolved filter
   - streak logic
   - formatting precision
2. model validation tests with sample payload fixtures.

### 16.2 Contract Tests

1. hit local/staging `/paper/*` endpoints and verify parseability.
2. ensure no required fields missing for all versions.

### 16.3 Integration/UI Tests

1. version toggle updates all query keys and screen data.
2. leaderboard sorting behavior.
3. trade row includes Buy Odds.
4. stale cache behavior under offline mode.

### 16.4 Visual Regression

1. snapshot tests for cards and leaderboard rows.
2. chart rendering smoke checks with deterministic fixtures.

---

## 17. Rollout Plan

### Phase 0: Contract Freeze and Fixtures (1-2 days)
1. Generate payload fixtures from live endpoints for v1/v2/all.
2. Lock mobile model interfaces.

### Phase 1: Foundation (2-3 days)
1. App scaffold, navigation, query client, endpoint clients.
2. Global version state and polling.

### Phase 2: Feature Parity (4-6 days)
1. Overview tab + leaderboard + charts.
2. Strategy detail screen.
3. Trades tab and formatting.
4. Health diagnostics tab.

### Phase 3: Hardening (2-3 days)
1. Offline cache polish.
2. error boundaries and retry UX.
3. telemetry and profiling.

### Phase 4: Release (1-2 days)
1. Beta rollout to internal users.
2. compare web/mobile numbers for parity signoff.

---

## 18. Acceptance Criteria

A release is acceptable when all are true:

1. Mobile app displays the same core KPIs as web for same selected version.
2. Buy Odds from `entry_ask` appears in mobile trade views.
3. V1/V2 toggling updates health, compare, and ledger consistently.
4. Leaderboard values match backend compare payload fields exactly.
5. Charts and trade lists render without frame drops on typical payload sizes.
6. Offline fallback shows last known good dataset with stale indicator.
7. No P0/P1 contract validation issues in telemetry during beta.

---

## 19. Risks and Mitigations

### Risk 1: Metric Drift Between Web and Mobile

- **Cause:** duplicated business logic or formula mismatch.
- **Mitigation:** codify shared selector formulas in tests against fixture snapshots.

### Risk 2: Payload Growth Degrades Trade List Performance

- **Cause:** full list render and repeated sorting.
- **Mitigation:** FlashList, memoization, and optional backend paging later.

### Risk 3: Split Inference Confusion

- **Cause:** users unclear on `split_mode` meaning.
- **Mitigation:** add tooltip/help copy in Health tab and split badge.

### Risk 4: Contract Changes Without Mobile Updates

- **Cause:** backend evolves fields silently.
- **Mitigation:** schema validation + alerting + compatibility policy.

---

## 20. Open Questions

1. Do we want `all` mode in mobile toggle or keep strict V1/V2 only (current web shows V1/V2 buttons)?
2. Should diagnostics fields (`audit_*`, `original_*`, `csv_dir`) be hidden behind a developer switch?
3. Is there a preferred chart package already approved in the repo ecosystem?
4. Should we add push notifications for large drawdown or status error in phase 2?

---

## 21. Appendix: Endpoint Examples

### Health

```json
{
  "status": "ok",
  "version": "v2",
  "strategies": 7,
  "total_trades": 50,
  "version_counts": { "v1": 340, "v2": 50 },
  "split_mode": "gap",
  "cutover_timestamp": 1774811870,
  "cutover_iso": "2026-03-29T19:17:50+00:00",
  "running_since": "2026-03-29T18:00:00+00:00",
  "hours_elapsed": 26.4,
  "csv_dir": "/var/lib/polymarket/paper_logs"
}
```

### Ledger Item

```json
{
  "timestamp": 1774812000000,
  "strategy": "v15_brier_cb",
  "slug": "btc-up-or-down-march-30-8pm-et",
  "direction": "UP",
  "confidence": 0.73,
  "edge": 0.041,
  "regime": "trend_up",
  "entry_ask": 0.532,
  "result": "WIN",
  "pnl": 2.13,
  "bankroll": 92.44,
  "original_pnl": 2.13,
  "original_bankroll": 92.44,
  "audit_entry_stake": null,
  "audit_pnl_delta": 0.0,
  "is_audit_corrected": false,
  "brier_score": 0.184,
  "cb_paused": false,
  "version": "v2"
}
```

### Compare Item

```json
{
  "strategy": "v15_brier_cb",
  "color": "#66bb6a",
  "trades": 48,
  "wins": 31,
  "win_rate": 64.6,
  "total_pnl": 9.32,
  "roi_pct": 11.7,
  "trades_per_day": 22.1,
  "max_drawdown_pct": 8.4,
  "profit_factor": 1.58,
  "avg_win": 1.42,
  "avg_loss": -1.09,
  "brier_avg": 0.236,
  "cb_pauses": 2
}
```
