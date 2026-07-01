"""Deep analysis: v9 signal accuracy by date, hour, and market characteristics.
Compare Feb 12/16 (old data) vs Feb 17 (new data, overlaps with live trading).
"""
import sqlite3
import pandas as pd
import numpy as np
from scipy.stats import norm
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DB_PATH = 'polymarket_btc_data.db'
EST_OFFSET = -5

# Load v9 backtest trade log
df_log = pd.read_csv('regime_trade_log.csv')
print(f"Trade log: {len(df_log)} trades")
print(f"Columns: {list(df_log.columns)}")

# Parse market epoch from slug
df_log['epoch_s'] = df_log['market'].apply(lambda x: int(x.split('-')[-1]))
df_log['dt_est'] = df_log['epoch_s'].apply(
    lambda e: datetime.fromtimestamp(e, tz=timezone.utc) + timedelta(hours=EST_OFFSET))
df_log['date'] = df_log['dt_est'].apply(lambda d: d.strftime('%Y-%m-%d'))
df_log['hour_est'] = df_log['dt_est'].apply(lambda d: d.hour)

# ============================================================
# BY DATE
# ============================================================
print(f"\n{'='*70}")
print(f"PERFORMANCE BY DATE")
print(f"{'='*70}")
print(f"{'Date':>12s}  {'Trades':>6s}  {'Wins':>5s}  {'WR%':>6s}  {'P&L':>8s}  {'AvgConf':>7s}")
for date in sorted(df_log['date'].unique()):
    d = df_log[df_log['date'] == date]
    n = len(d)
    w = d['correct'].sum() if 'correct' in d.columns else (d['pnl'] > 0).sum()
    wr = w / n * 100
    pnl = d['pnl'].sum()
    conf = d['confidence'].mean() if 'confidence' in d.columns else 0
    print(f"{date:>12s}  {n:>5d}   {w:>4d}   {wr:>5.1f}%  ${pnl:>+7.2f}  {conf:>6.3f}")

# ============================================================
# BY HOUR (EST)
# ============================================================
print(f"\n{'='*70}")
print(f"PERFORMANCE BY HOUR (EST)")
print(f"{'='*70}")
print(f"{'Hour':>6s}  {'Trades':>6s}  {'Wins':>5s}  {'WR%':>6s}  {'P&L':>8s}")
for hour in sorted(df_log['hour_est'].unique()):
    h = df_log[df_log['hour_est'] == hour]
    n = len(h)
    w = (h['pnl'] > 0).sum()
    wr = w / n * 100
    pnl = h['pnl'].sum()
    print(f"{hour:>4d}h   {n:>5d}   {w:>4d}   {wr:>5.1f}%  ${pnl:>+7.2f}")

# ============================================================
# FEB 17 DETAIL (matches live trading window)
# ============================================================
feb17 = df_log[df_log['date'] == '2026-02-17'].copy()
print(f"\n{'='*70}")
print(f"FEB 17 DETAIL: {len(feb17)} trades")
print(f"{'='*70}")

if len(feb17) > 0:
    w = (feb17['pnl'] > 0).sum()
    l = len(feb17) - w
    print(f"W/L: {w}/{l} = {w/len(feb17)*100:.1f}% WR")
    print(f"Total P&L: ${feb17['pnl'].sum():.2f}")
    
    print(f"\n{'#':>3}  {'Time':10s}  {'Side':5s}  {'Entry':>7s}  {'P&L':>8s}  "
          f"{'Conf':>6s}  {'Edge':>6s}  {'Regime':>8s}  {'Result':>6s}")
    for i, (_, t) in enumerate(feb17.iterrows()):
        dt = datetime.fromtimestamp(t['epoch_s'], tz=timezone.utc) + timedelta(hours=EST_OFFSET)
        result = 'WIN' if t['pnl'] > 0 else 'LOSS'
        regime = t.get('regime', '?')
        edge = t.get('edge', 0)
        side = t.get('side', '?')
        conf = t.get('confidence', 0)
        entry = t.get('entry_price', 0)
        print(f"{i+1:>3}  {dt.strftime('%I:%M%p'):10s}  {side:5s}  ${entry:>6.3f}  "
              f"${t['pnl']:>+7.2f}  {conf:>5.3f}  {edge:>5.3f}  {regime:>8s}  {result:>6s}")

# ============================================================
# COMPARE LEDGER vs BACKTEST FOR FEB 17
# ============================================================
import json
try:
    with open('strategies/v9_regime_filter/ledger.json') as f:
        ledger = json.load(f)
    
    v9_live = [t for t in ledger if t.get('signal_version') == 'v9-regime']
    
    # Deduplicate live by slug
    live_by_slug = {}
    for t in v9_live:
        slug = t['slug']
        if slug not in live_by_slug:
            live_by_slug[slug] = {
                'won': t['won'], 'outcome': t['outcome'],
                'avg_price': t['avg_price'], 'pnl': t['cash_pnl'],
                'fills': 1, 'total_invested': t['initial_value'],
                'total_pnl': t['cash_pnl'],
            }
        else:
            live_by_slug[slug]['fills'] += 1
            live_by_slug[slug]['total_invested'] += t['initial_value']
            live_by_slug[slug]['total_pnl'] += t['cash_pnl']
    
    # Match live vs backtest
    print(f"\n{'='*70}")
    print(f"LIVE vs BACKTEST COMPARISON (Feb 17 markets)")
    print(f"{'='*70}")
    
    backtest_slugs = set(feb17['market'].values) if len(feb17) > 0 else set()
    live_slugs = set(live_by_slug.keys())
    
    both = backtest_slugs & live_slugs
    bt_only = backtest_slugs - live_slugs
    live_only = live_slugs - backtest_slugs
    
    print(f"In both: {len(both)}")
    print(f"Backtest only: {len(bt_only)}")
    print(f"Live only: {len(live_only)}")
    
    if both:
        agree = 0
        disagree = 0
        for slug in sorted(both):
            bt_row = feb17[feb17['market'] == slug].iloc[0]
            live = live_by_slug[slug]
            bt_won = bt_row['pnl'] > 0
            live_won = live['won']
            
            match = 'AGREE' if bt_won == live_won else 'DISAGREE'
            if bt_won == live_won:
                agree += 1
            else:
                disagree += 1
                
            epoch = int(slug.split('-')[-1])
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc) + timedelta(hours=EST_OFFSET)
            
            bt_side = bt_row.get('side', '?')
            live_side = live['outcome']
            fills = live['fills']
            fill_marker = f' ({fills} fills!)' if fills > 1 else ''
            
            print(f"  {dt.strftime('%I:%M%p'):8s}  BT={bt_side:4s}{'WIN' if bt_won else 'LOSS':>5s}  "
                  f"Live={live_side:4s}{'WIN' if live_won else 'LOSS':>5s}  "
                  f"{match:8s}{fill_marker}")
        
        print(f"\n  Agreement: {agree}/{len(both)} = {agree/len(both)*100:.0f}%")
        print(f"  Disagreement: {disagree}/{len(both)} = {disagree/len(both)*100:.0f}%")

except FileNotFoundError:
    print("\nLedger not found, skipping live comparison.")

# ============================================================
# REGIME DISTRIBUTION
# ============================================================
if 'regime' in df_log.columns:
    print(f"\n{'='*70}")
    print(f"REGIME ANALYSIS")
    print(f"{'='*70}")
    for regime in df_log['regime'].unique():
        r = df_log[df_log['regime'] == regime]
        n = len(r)
        w = (r['pnl'] > 0).sum()
        wr = w / n * 100
        pnl = r['pnl'].sum()
        print(f"  {regime:8s}: {w}/{n} = {wr:.1f}% WR  P&L=${pnl:+.2f}")

# ============================================================
# SIDE ANALYSIS
# ============================================================
if 'side' in df_log.columns:
    print(f"\n{'='*70}")
    print(f"SIDE ANALYSIS")
    print(f"{'='*70}")
    for side in ['UP', 'DOWN']:
        s = df_log[df_log['side'] == side]
        if len(s) == 0:
            continue
        n = len(s)
        w = (s['pnl'] > 0).sum()
        wr = w / n * 100
        pnl = s['pnl'].sum()
        avg_entry = s['entry_price'].mean()
        avg_conf = s['confidence'].mean() if 'confidence' in s.columns else 0
        print(f"  {side:5s}: {w}/{n} = {wr:.1f}% WR  P&L=${pnl:+.2f}  "
              f"avg_entry=${avg_entry:.3f}  avg_conf={avg_conf:.3f}")

    # By date + side
    print(f"\n  By Date + Side:")
    for date in sorted(df_log['date'].unique()):
        for side in ['UP', 'DOWN']:
            mask = (df_log['date'] == date) & (df_log['side'] == side)
            s = df_log[mask]
            if len(s) == 0:
                continue
            n = len(s)
            w = (s['pnl'] > 0).sum()
            wr = w / n * 100
            pnl = s['pnl'].sum()
            print(f"    {date} {side:5s}: {w}/{n} = {wr:.1f}% WR  P&L=${pnl:+.2f}")
