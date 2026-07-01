"""
Live Ledger Analysis vs V9 Backtest
====================================
Fetches the live ledger, parses v9-regime trades, runs the v9 backtest on
the same date range (using fresh DB data), and compares the two.

Usage:  python analyze_ledger_v9.py
"""

import json
import urllib.request
import sqlite3
import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import warnings
warnings.filterwarnings('ignore')

LEDGER_URL = os.environ.get("OPENMARKET_LEDGER_URL", "http://localhost:8006/ledger")
DB_PATH = "polymarket_btc_data.db"

# ── Pull ledger ────────────────────────────────────────────────────────────────
print("Fetching live ledger...")
with urllib.request.urlopen(LEDGER_URL) as r:
    ledger = json.loads(r.read().decode())
print(f"  {len(ledger)} raw redemption records")

# ── Keep only v9-regime trades, SUCCESS, that actually moved money ─────────────
v9_all = [t for t in ledger if t.get("signal_version") == "v9-regime"]
v9_success = [t for t in v9_all if t.get("tx_status") == "SUCCESS"]

# Deduplicate by condition_id (take latest redeemed_at per condition)
seen = {}
for t in v9_success:
    cid = t["condition_id"]
    if cid not in seen or t["redeemed_at"] > seen[cid]["redeemed_at"]:
        seen[cid] = t
v9_deduped = list(seen.values())
v9_deduped.sort(key=lambda x: x["redeemed_at"])

print(f"\n=== LIVE v9-regime Trades (SUCCESS, deduped) ===")
print(f"  Total v9 records:        {len(v9_all)}")
print(f"  Successful redemptions:  {len(v9_success)}")
print(f"  Unique positions:        {len(v9_deduped)}")

# Stats
wins    = [t for t in v9_deduped if t["won"]]
losses  = [t for t in v9_deduped if not t["won"]]
total_pnl = sum(t["cash_pnl"] for t in v9_deduped)
wr = len(wins) / len(v9_deduped) * 100 if v9_deduped else 0

print(f"  Win rate:                {len(wins)}/{len(v9_deduped)} = {wr:.1f}%")
print(f"  Total P&L:               ${total_pnl:+.2f}")

# Equity curve
start_usdc = v9_deduped[0]["usdc_before"] if v9_deduped else 0
end_usdc   = v9_deduped[-1]["usdc_after"]  if v9_deduped else 0
peak_usdc  = max(t["usdc_after"] for t in v9_deduped)

print(f"  Start USDC:              ${start_usdc:.2f}")
print(f"  Peak USDC:               ${peak_usdc:.2f}")
print(f"  End USDC:                ${end_usdc:.2f}")
print(f"  Net v9 change:           ${end_usdc - start_usdc:+.2f}")

# Per-trade breakdown
print(f"\n{'─'*90}")
print(f"  {'Time (UTC)':22s}  {'Market':35s}  {'Side':5s}  {'Avg$':5s}  {'Size':6s}  {'PnL':>8s}  {'W?'}")
print(f"{'─'*90}")
for t in v9_deduped:
    dt = t["redeemed_at"][:19].replace("T"," ")
    title_short = t["title"].replace("Bitcoin Up or Down - ","")[:34]
    side = t["outcome"]
    price = t["avg_price"]
    size = t["size"]
    pnl = t["cash_pnl"]
    w = "✓" if t["won"] else "✗"
    print(f"  {dt:22s}  {title_short:35s}  {side:5s}  {price:.3f}  {size:6.2f}  ${pnl:>7.2f}  {w}")

# Hour-of-day breakdown (UTC → ET = UTC-5)
print(f"\n=== LIVE v9 Performance by Hour (ET) ===")
hour_stats = {}
for t in v9_deduped:
    # redeemed_at is resolve time; slug epoch = entry time
    slug = t["slug"]
    epoch_s = int(slug.split("-")[-1])
    dt_utc = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    hour_et = (dt_utc.hour - 5) % 24  # ET = UTC-5
    h = hour_et
    if h not in hour_stats:
        hour_stats[h] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
    hour_stats[h]["trades"] += 1
    hour_stats[h]["pnl"] += t["cash_pnl"]
    if t["won"]:
        hour_stats[h]["wins"] += 1
    else:
        hour_stats[h]["losses"] += 1

print(f"  {'Hour(ET)':10s}  {'Trades':7s}  {'WR%':7s}  {'P&L':>10s}")
print(f"  {'─'*10}  {'─'*7}  {'─'*7}  {'─'*10}")
for h in sorted(hour_stats.keys()):
    s = hour_stats[h]
    wr_h = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
    flag = " ← BAD" if wr_h < 40 else (" ← GREAT" if wr_h > 75 else "")
    print(f"  {h:02d}:xx       {s['trades']:>5d}   {wr_h:>6.1f}%  ${s['pnl']:>+8.2f}{flag}")

# Entry price distribution
print(f"\n=== LIVE v9 Entry Price Distribution ===")
price_buckets = {}
for t in v9_deduped:
    p = t["avg_price"]
    bucket = round(int(p * 10) / 10, 1)
    if bucket not in price_buckets:
        price_buckets[bucket] = {"wins": 0, "losses": 0, "pnl": 0.0}
    price_buckets[bucket]["pnl"] += t["cash_pnl"]
    if t["won"]:
        price_buckets[bucket]["wins"] += 1
    else:
        price_buckets[bucket]["losses"] += 1

print(f"  {'Price':8s}  {'Trades':7s}  {'WR%':7s}  {'P&L':>10s}")
print(f"  {'─'*8}  {'─'*7}  {'─'*7}  {'─'*10}")
for p in sorted(price_buckets.keys()):
    s = price_buckets[p]
    total = s["wins"] + s["losses"]
    wr_p = s["wins"] / total * 100 if total > 0 else 0
    flag = " ← BAD" if wr_p < 40 and total > 1 else ""
    print(f"  {p:.1f}–{p+0.1:.1f}     {total:>5d}   {wr_p:>6.1f}%  ${s['pnl']:>+8.2f}{flag}")

# ── What slugs were active in live v9 period? ─────────────────────────────────
v9_slugs = sorted(set(t["slug"] for t in v9_deduped))
v9_epochs = [int(s.split("-")[-1]) for s in v9_slugs]
ts_min = min(v9_epochs)
ts_max = max(v9_epochs)

print(f"\n=== Date Range ===")
print(f"  First market start: {datetime.fromtimestamp(ts_min, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print(f"  Last market start:  {datetime.fromtimestamp(ts_max, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

# ── Run V9 Backtest on same date range from fresh DB ─────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "strategies", "v9_regime_filter"))
from backtest_regime_v9 import (
    load_all_data, build_market_signals, backtest_hold_to_resolve,
    compute_stats, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE,
    MAX_ENTRY_PRICE
)

print(f"\n{'='*70}")
print(f" Running V9 Backtest on Fresh DB (all markets overlapping live session)")
print(f"{'='*70}")

conn = sqlite3.connect(DB_PATH)
df_meta = pd.read_sql_query(
    "SELECT * FROM market_meta ORDER BY first_seen_ms ASC", conn)
df_ticks = pd.read_sql_query(
    """SELECT market_slug, source_ts_ms, side_label, price, best_bid, best_ask, size, event_type
       FROM polymarket_ticks_ms ORDER BY source_ts_ms ASC""", conn)
df_trades = pd.read_sql_query(
    "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC",
    conn)
conn.close()

# Filter to markets covering the live session ± 1 hour
window_start = ts_min - 3600
window_end   = ts_max + 3600
df_meta_filt = df_meta[
    df_meta['market_slug'].apply(lambda s: window_start <= int(s.split('-')[-1]) <= window_end)
].copy()

print(f"  Markets in window: {len(df_meta_filt)}")

# Filter trades/ticks to same window (for speed)
start_ms = window_start * 1000
end_ms   = (window_end + 900) * 1000
df_trades_filt = df_trades[
    (df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < end_ms)
].copy()
df_ticks_filt = df_ticks[
    (df_ticks['source_ts_ms'] >= start_ms) & (df_ticks['source_ts_ms'] < end_ms)
].copy()

print(f"  Binance trades in window: {len(df_trades_filt):,}")
print(f"  Polymarket ticks in window: {len(df_ticks_filt):,}")

df_signals, signals_full = build_market_signals(df_meta_filt, df_trades_filt, df_ticks_filt)

if len(signals_full) == 0:
    print("  !! No signals generated — check DB coverage for this date range")
    sys.exit(1)

# Run multiple confidence thresholds
print(f"\n=== Backtest Sweep (same live date range) ===")
print(f"  {'Conf':>6s}  {'Edge':>5s}  {'Trades':>6s}  {'WR%':>6s}  {'ROI':>8s}  {'Final':>8s}  {'MDD':>6s}")
print(f"  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*8}  {'─'*8}  {'─'*6}")
sweep_results = []
for conf in [0.55, 0.60, 0.65, 0.70]:
    for edge in [0.00, 0.03, 0.05]:
        log, ec, final = backtest_hold_to_resolve(
            signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE,
            conf, min_edge=edge, max_price=MAX_ENTRY_PRICE)
        if len(log) > 0:
            s = compute_stats(log, final)
            sweep_results.append({'conf': conf, 'edge': edge, **s})
            print(f"  {conf:>5.0%}   {edge:>3.0%}   {s['trades']:>5d}   "
                  f"{s['wr']:>5.1f}%  {s['roi']:>+7.1f}%  ${s['final']:>7.2f}  "
                  f"{s['mdd']:>5.1%}")

# Best backtest result
best = max(sweep_results, key=lambda x: x['final']) if sweep_results else None

# ── Reconciliation ─────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f" LIVE vs BACKTEST RECONCILIATION")
print(f"{'='*70}")
print(f"\n  LIVE v9 (deduped, SUCCESS only):")
print(f"    Trades:      {len(v9_deduped)}")
print(f"    Win rate:    {wr:.1f}%")
print(f"    Net P&L:     ${total_pnl:+.2f}")
print(f"    Start USDC:  ${start_usdc:.2f}")
print(f"    Peak USDC:   ${peak_usdc:.2f}")
print(f"    End USDC:    ${end_usdc:.2f}")
print(f"    Net change:  ${end_usdc - start_usdc:+.2f}")

if best:
    print(f"\n  BACKTEST (best: conf={best['conf']:.0%}, edge={best['edge']:.0%}):")
    print(f"    Trades:      {best['trades']}")
    print(f"    Win rate:    {best['wr']:.1f}%")
    print(f"    Net P&L:     ${best['pnl']:+.2f}")
    print(f"    Final:       ${best['final']:.2f}")
    print(f"    ROI:         {best['roi']:+.1f}%")
    print(f"    MDD:         {best['mdd']:.1%}")
    print(f"    PF:          {best['profit_factor']:.2f}")

print(f"\n=== GAP ANALYSIS: Why live underperforms backtest ===")
print(f"""
Key factors observed:
  1. BAD HOURS (live): Check which ET hours had <40% WR in live data above
  2. ENTRY PRICE QUALITY: High avg_price (>0.65) = less edge = volatility kills you
  3. BACKTEST SIGNAL SELECTION BIAS: Backtest sees the best market in hindsight;
     live loop may enter earlier or with less clear regime
  4. FEE DRAG: Each round-trip costs ~2% of bet; compounds over 60+ trades
  5. SLIPPAGE: Actual fill may be worse than best_ask + 0.005 assumed
  6. TX FAILURES (GAS): Already accounted for — user says ignore
  7. REGIME DETECTION LATENCY: Live system sees less data at entry vs backtest
""")

# ── Best backtest signal detail ────────────────────────────────────────────────
if best:
    conf_best = best['conf']
    edge_best = best['edge']
    log_best, _, _ = backtest_hold_to_resolve(
        signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE,
        conf_best, min_edge=edge_best, max_price=MAX_ENTRY_PRICE)

    print(f"=== Backtest Signals (conf={conf_best:.0%}, edge={edge_best:.0%}) ===")
    print(f"  {'Market':40s}  {'Side':4s}  {'Entry$':7s}  {'Conf':6s}  {'Edge':5s}  {'Reg':7s}  {'Secs':5s}  {'W?'}")
    print(f"  {'─'*40}  {'─'*4}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*7}  {'─'*5}  {'─'*3}")

    bt_by_slug = {}
    for sig in signals_full:
        ep = int(sig['slug'].split('-')[-1])
        if window_start <= ep <= window_end:
            bt_by_slug[sig['slug']] = sig

    for sig in signals_full:
        conf_sig = sig['confidence']
        side = sig['signal']
        ep = sig['entry_up_ask'] if side == 'UP' else sig['entry_down_ask']
        edge_sig = conf_sig - (ep + SLIPPAGE)
        if conf_sig >= conf_best and edge_sig >= edge_best:
            ep_dt = datetime.fromtimestamp(int(sig['slug'].split('-')[-1]), tz=timezone.utc)
            ep_et = f"{(ep_dt.hour-5)%24:02d}:{ep_dt.minute:02d} ET"
            w = "✓" if sig['signal'] == sig['actual'] else "✗"
            reg = sig.get('regime', '?')[:7]
            secs = sig.get('entry_secs_in', 0)
            mkt_short = sig['slug'][-12:]
            print(f"  {mkt_short:40s}  {side:4s}  {ep:.3f}    {conf_sig:.3f}  {edge_sig:.3f}  {reg:7s}  {secs:5d}  {w}")

    # Save
    pd.DataFrame(log_best).to_csv("v9_analysis_trade_log.csv", index=False)
    print(f"\n  Backtest trade log saved to v9_analysis_trade_log.csv")
    
print(f"\nDone.")
