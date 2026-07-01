"""
COMPREHENSIVE POLYMARKET BTC 15-MIN BACKTESTER
================================================
Two strategies, both using our ML ensemble signals:

STRATEGY A: HOLD-TO-RESOLVE
  - Buy UP/DOWN contract early in the market window
  - Hold until resolution (15 min mark)
  - Correct → payout $1.00 per share
  - Wrong → payout $0.00 per share
  - Edge comes from buying at <0.50 and being right >50% of the time

STRATEGY B: MOMENTUM CAPTURE
  - Buy contract, set take-profit target
  - If price moves in our favor, sell for profit
  - If we reach resolution without hitting target, let it resolve

EXECUTION MODEL:
  - Entry: Buy at ASK price + slippage
  - Exit:  Sell at BID price - slippage
  - Fees: 1% of notional on both entry and exit
  - Slippage: configurable (default 0.5 cent per share)

BANKROLL:
  - Start: $100
  - Bet size: 5% of bankroll per trade
  - Full reinvestment

OUTPUT:
  - Detailed trade log (CSV)
  - Bankroll curve chart (PNG)
  - Performance statistics
"""

import os
import sqlite3
import pandas as pd
import numpy as np
import xgboost as xgb
import lightgbm as lgb
from sklearn.metrics import accuracy_score
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
import warnings
warnings.filterwarnings('ignore')
import time
import json

DB_PATH = 'polymarket_btc_data.db'

# ========================================================================
# CONFIG
# ========================================================================
INITIAL_BANKROLL = 100.0
BET_FRACTION     = 0.05       # 5% of bankroll per trade
SLIPPAGE         = 0.005      # $0.005 per share (half a cent)
FEE_RATE         = 0.01       # 1% fee on each leg
MIN_CONFIDENCE   = 0.55       # Don't trade if ML confidence < this
MOMENTUM_TARGETS = [0.05, 0.10, 0.15, 0.20]  # Take-profit targets to test

# ========================================================================
# DATA LOADING (reuse from our pipeline)
# ========================================================================

def load_all_data():
    conn = sqlite3.connect(DB_PATH)
    
    # Market metadata
    df_meta = pd.read_sql_query("SELECT * FROM market_meta ORDER BY first_seen_ms ASC", conn)
    
    # All Polymarket ticks
    df_ticks = pd.read_sql_query(
        """SELECT market_slug, source_ts_ms, side_label, price, best_bid, best_ask, size, event_type
           FROM polymarket_ticks_ms ORDER BY source_ts_ms ASC""", conn)
    
    # Binance trades for ground truth
    df_trades = pd.read_sql_query(
        "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC", conn)
    
    # Lag pairs
    df_lag = pd.read_sql_query(
        "SELECT paired_at_ms, lead_lag_ms, quality_flag FROM lag_pairs_ms ORDER BY paired_at_ms ASC", conn)
    
    conn.close()
    return df_meta, df_ticks, df_trades, df_lag

# ========================================================================
# ML MODEL (simplified — reuse our best pipeline for 1s signals)
# ========================================================================

def agg_binance(df, ms):
    df = df.copy()
    df['b'] = (df['trade_time'] // ms) * ms
    is_buy = df['is_buyer_maker'] == 0
    g = df.groupby('b')
    a = g['price'].agg(o='first', h='max', l='min', c='last', pstd='std').reset_index()
    a['v'] = g['quantity'].sum().values
    a['tc'] = g['price'].count().values
    a['ats'] = g['quantity'].mean().values
    a['mts'] = g['quantity'].max().values
    bv = df[is_buy].groupby('b')['quantity'].sum().rename('bv')
    sv = df[~is_buy].groupby('b')['quantity'].sum().rename('sv')
    a = a.merge(bv, on='b', how='left').merge(sv, on='b', how='left')
    a[['bv','sv']] = a[['bv','sv']].fillna(0)
    df['pv'] = df['price'] * df['quantity']
    vw = (df.groupby('b')['pv'].sum() / df.groupby('b')['quantity'].sum()).rename('vwap')
    a = a.merge(vw, on='b', how='left')
    a['ts'] = pd.to_datetime(a['b'], unit='ms')
    a.set_index('ts', inplace=True)
    a.drop(columns=['b'], inplace=True)
    return a

def agg_poly_for_ml(df_ticks, ms):
    df = df_ticks[df_ticks['event_type'] == 'price_change'].copy()
    df['b'] = (df['source_ts_ms'] // ms) * ms
    r = pd.DataFrame()
    for s in ['UP', 'DOWN']:
        sub = df[df['side_label'] == s]
        if len(sub) == 0: continue
        g = sub.groupby('b')
        sl = s.lower()
        a = pd.DataFrame({
            f'p_{sl}_last': g['price'].last(), f'p_{sl}_bid': g['best_bid'].last(),
            f'p_{sl}_ask': g['best_ask'].last(), f'p_{sl}_vol': g['size'].sum(),
            f'p_{sl}_cnt': g['price'].count(),
        })
        r = a if r.empty else r.join(a, how='outer')
    if r.empty: return pd.DataFrame()
    r.index = pd.to_datetime(r.index, unit='ms')
    return r

def agg_lag(df, ms):
    df = df[df['quality_flag'].isin(['tight','medium'])].copy()
    if len(df) == 0: return pd.DataFrame()
    df['b'] = (df['paired_at_ms'] // ms) * ms
    g = df.groupby('b')
    a = pd.DataFrame({
        'lgm': g['lead_lag_ms'].mean(), 'lgstd': g['lead_lag_ms'].std(),
        'lgpr': g['lead_lag_ms'].apply(lambda x: (x>0).mean()),
        'lgn': g['lead_lag_ms'].count(),
    })
    a.index = pd.to_datetime(a.index, unit='ms')
    return a

def build_features(b1, p1, l1, b5, p5):
    df = b1.copy()
    if not p1.empty: df = df.join(p1, how='left')
    if not l1.empty: df = df.join(l1, how='left')
    if not b5.empty:
        b5r = b5[['c','v','tc']].copy()
        b5r.columns = ['c5s','v5s','tc5s']
        df = df.join(b5r, how='left')
        df[['c5s','v5s','tc5s']] = df[['c5s','v5s','tc5s']].ffill()
    if not p5.empty:
        p5r = p5[['p_up_last','p_up_vol']].copy()
        p5r.columns = ['p_up_5s','p_up_vol_5s']
        df = df.join(p5r, how='left')
        df[['p_up_5s','p_up_vol_5s']] = df[['p_up_5s','p_up_vol_5s']].ffill()
    pcols = [c for c in df.columns if c.startswith('p_') or c.startswith('lg')]
    df[pcols] = df[pcols].ffill().fillna(0)

    df['ret'] = df['c'].pct_change()
    df['hl'] = (df['h']-df['l'])/(df['c']+1e-9)
    df['co'] = (df['c']-df['o'])/(df['o']+1e-9)
    df['vwap_d'] = (df['c']-df['vwap'])/(df['vwap']+1e-9)
    df['ivol'] = df['pstd']/(df['c']+1e-9)
    tot = df['bv']+df['sv']+1e-9
    df['ofi'] = (df['bv']-df['sv'])/tot
    df['br'] = df['bv']/tot
    for w in [3,5,10]:
        df[f'ofi_m{w}'] = df['ofi'].rolling(w,min_periods=1).mean()
        df[f'ofi_a{w}'] = df['ofi']-df[f'ofi_m{w}']
    df['cum_ofi'] = df['ofi'].rolling(30,min_periods=1).sum()
    df['tc_r'] = df['tc'].pct_change()
    df['tc_m5'] = df['tc'].rolling(5,min_periods=1).mean()
    df['rtc'] = df['tc']/(df['tc_m5']+1e-9)
    df['ats_m'] = df['ats'].rolling(10,min_periods=1).mean()
    df['rats'] = df['ats']/(df['ats_m']+1e-9)
    df['whale'] = df['mts']/(df['mts'].rolling(10,min_periods=1).mean()+1e-9)
    df['v3'] = df['ret'].rolling(3,min_periods=1).std()
    df['v10'] = df['ret'].rolling(10,min_periods=1).std()
    df['vratio'] = df['v3']/(df['v10']+1e-9)
    for p in [3,5,10]:
        df[f'roc{p}'] = df['c'].pct_change(periods=p)
    d = df['c'].diff()
    gn = d.where(d>0,0).rolling(10,min_periods=1).mean()
    ls = (-d.where(d<0,0)).rolling(10,min_periods=1).mean()
    df['rsi'] = 100-(100/(1+gn/(ls+1e-9)))
    df['ema_x'] = (df['c'].ewm(span=5).mean()-df['c'].ewm(span=15).mean())/(df['c'].ewm(span=15).mean()+1e-9)
    if 'p_up_last' in df.columns:
        df['pup'] = df['p_up_last']
        df['psp_u'] = df.get('p_up_ask',0)-df.get('p_up_bid',0)
        df['psp_d'] = df.get('p_down_ask',0)-df.get('p_down_bid',0)
        df['pm3'] = df['p_up_last'].pct_change(3)
        df['pm5'] = df['p_up_last'].pct_change(5)
        df['pd1'] = df['p_up_last'].diff()
        if 'p_up_vol' in df.columns and 'p_down_vol' in df.columns:
            ptv = df['p_up_vol']+df['p_down_vol']+1e-9
            df['pvr'] = df['p_up_vol']/ptv
        df['pdiv'] = df['p_up_last'].pct_change()-df['ret']
    if 'lgm' in df.columns:
        df['lgdir'] = np.sign(df['lgm'])
        df['lgchg'] = df['lgm'].diff()
    df['hour'] = df.index.hour
    df['hour_sin'] = np.sin(2*np.pi*df['hour']/24)
    df['hour_cos'] = np.cos(2*np.pi*df['hour']/24)
    if 'c5s' in df.columns:
        df['ret_5s'] = df['c5s'].pct_change()
        df['cross_tf'] = df['ret']-df['ret_5s']
    for i in range(1,6):
        df[f'rl{i}'] = df['ret'].shift(i)
        df[f'ol{i}'] = df['ofi'].shift(i)
    if 'pup' in df.columns:
        for i in range(1,4):
            df[f'pl{i}'] = df['pup'].shift(i)
    df['target'] = (df['c'].shift(-1)>df['c']).astype(int)
    df = df.iloc[10:-1]
    fcols = [c for c in df.columns if any(c.startswith(p) for p in ['p_','lg','c5s','v5s','tc5s','p_up_5s','p_up_vol_5s'])]
    if fcols: df[fcols] = df[fcols].ffill().fillna(0)
    df = df.fillna(0).replace([np.inf,-np.inf],0)
    return df

def get_feats(df):
    raw = {'target','o','h','l','c','v','bv','sv','vwap','pstd','ats','mts','tc',
           'ret','tc_m5','ats_m','p_up_last','p_up_bid','p_up_ask','p_down_last','p_down_bid',
           'p_down_ask','p_up_vol','p_down_vol','p_up_cnt','p_down_cnt','lgn','lgstd',
           'c5s','v5s','tc5s','p_up_5s','p_up_vol_5s','hour','p_up_mean','p_down_mean','ret_5s'}
    return [c for c in df.columns if c not in raw]

def train_ensemble(df):
    """Train our stacking ensemble and return the prediction function."""
    feats = get_feats(df)
    X, y = df[feats], df['target']
    n = len(df)
    tr = int(n * 0.7)
    vl = int(n * 0.85)
    
    Xtr, ytr = X.iloc[:tr], y.iloc[:tr]
    Xvl, yvl = X.iloc[tr:vl], y.iloc[tr:vl]
    spw = (ytr==0).sum()/((ytr==1).sum()+1e-9)
    
    # XGBoost
    xgb_m = xgb.XGBClassifier(
        n_estimators=2000,max_depth=4,learning_rate=0.01,subsample=0.7,
        colsample_bytree=0.5,gamma=0.5,min_child_weight=3,reg_alpha=0,
        reg_lambda=0.5,scale_pos_weight=spw,eval_metric='logloss',
        random_state=42,early_stopping_rounds=30)
    xgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
    
    # LightGBM
    lgb_m = lgb.LGBMClassifier(
        n_estimators=2000,max_depth=4,learning_rate=0.01,subsample=0.7,
        colsample_bytree=0.5,min_child_weight=3,reg_alpha=0,reg_lambda=0.5,
        scale_pos_weight=spw,random_state=42,verbose=-1,n_jobs=1)
    lgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], callbacks=[lgb.early_stopping(30, verbose=False)])
    
    # Stacking meta-learner
    xgb_vp = xgb_m.predict_proba(Xvl)[:,1]
    lgb_vp = lgb_m.predict_proba(Xvl)[:,1]
    meta_X = np.column_stack([lgb_vp, xgb_vp])
    meta_clf = LogisticRegression(C=1.0, random_state=42)
    meta_clf.fit(meta_X, yvl)
    
    print(f"  Models trained. XGB iter={xgb_m.best_iteration}, LGB iter={lgb_m.best_iteration_}")
    
    return xgb_m, lgb_m, meta_clf, feats

def predict_ensemble(xgb_m, lgb_m, meta_clf, X):
    """Get ensemble UP probability for each row."""
    xp = xgb_m.predict_proba(X)[:,1]
    lp = lgb_m.predict_proba(X)[:,1]
    meta_X = np.column_stack([lp, xp])
    prob = meta_clf.predict_proba(meta_X)[:,1]
    return prob

# ========================================================================
# MARKET-LEVEL SIGNAL AGGREGATION
# ========================================================================

def build_market_signals(df_meta, df_trades, df_ticks, df_lag):
    """
    For each 15-min market, aggregate 1s ML signals into a market-level prediction.
    Returns a DataFrame with one row per market: signal direction, confidence, entry prices.
    """
    print("\n  Training ML ensemble on 1s data...")
    
    # Build 1s features
    p_ticks = df_ticks[df_ticks['event_type'] == 'price_change'].copy()
    b1 = agg_binance(df_trades, 1000)
    p1 = agg_poly_for_ml(df_ticks, 1000)
    l1 = agg_lag(df_lag, 1000)
    b5 = agg_binance(df_trades, 5000)
    p5 = agg_poly_for_ml(df_ticks, 5000)
    df_ml = build_features(b1, p1, l1, b5, p5)
    print(f"  ML dataset: {len(df_ml)} rows")
    
    feats = get_feats(df_ml)
    xgb_m, lgb_m, meta_clf, _ = train_ensemble(df_ml)
    
    # Now, for each market window, aggregate the ML predictions
    signals = []
    
    for _, market in df_meta.iterrows():
        slug = market['market_slug']
        epoch_s = int(slug.split('-')[-1])
        start_ms = epoch_s * 1000
        end_ms = start_ms + 900_000  # 15 min
        
        # Get BTC ground truth direction
        window_trades = df_trades[(df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < end_ms)]
        if len(window_trades) < 10:
            continue
        
        btc_start = window_trades.iloc[0]['price']
        btc_end = window_trades.iloc[-1]['price']
        actual_direction = 'UP' if btc_end > btc_start else 'DOWN'
        
        # Get ML predictions for this window (use first 5 minutes for signal)
        signal_end_ms = start_ms + 300_000  # First 5 minutes
        signal_start = pd.to_datetime(start_ms, unit='ms')
        signal_end = pd.to_datetime(signal_end_ms, unit='ms')
        
        window_ml = df_ml[(df_ml.index >= signal_start) & (df_ml.index < signal_end)]
        
        if len(window_ml) < 1:
            continue
        
        # Get probabilities
        X_window = window_ml[feats]
        probs = predict_ensemble(xgb_m, lgb_m, meta_clf, X_window)
        
        # Aggregate: mean probability the next tick goes UP
        mean_up_prob = probs.mean()
        
        # Signal: if mean_up_prob > 0.5 → we think UP, else DOWN
        if mean_up_prob > 0.5:
            signal_dir = 'UP'
            confidence = mean_up_prob
        else:
            signal_dir = 'DOWN'
            confidence = 1 - mean_up_prob
        
        # Get Polymarket entry prices (what we'd actually buy at)
        entry_window_ticks = p_ticks[
            (p_ticks['market_slug'] == slug) & 
            (p_ticks['source_ts_ms'] >= start_ms) &
            (p_ticks['source_ts_ms'] < signal_end_ms)
        ]
        
        up_ticks_w = entry_window_ticks[entry_window_ticks['side_label'] == 'UP']
        down_ticks_w = entry_window_ticks[entry_window_ticks['side_label'] == 'DOWN']
        
        entry_up_ask = up_ticks_w['best_ask'].median() if len(up_ticks_w) > 0 else 0.50
        entry_down_ask = down_ticks_w['best_ask'].median() if len(down_ticks_w) > 0 else 0.50
        
        # Get ALL ticks for this market (for momentum strategy - price evolution)
        all_market_ticks = p_ticks[p_ticks['market_slug'] == slug].copy()
        all_market_ticks = all_market_ticks[all_market_ticks['source_ts_ms'] >= signal_end_ms]  # after our entry
        
        # Price trajectory for momentum exits
        up_trajectory = all_market_ticks[all_market_ticks['side_label'] == 'UP'][['source_ts_ms','best_bid','best_ask','price']].copy()
        down_trajectory = all_market_ticks[all_market_ticks['side_label'] == 'DOWN'][['source_ts_ms','best_bid','best_ask','price']].copy()
        
        signals.append({
            'slug': slug,
            'start_ms': start_ms,
            'end_ms': end_ms,
            'btc_start': btc_start,
            'btc_end': btc_end,
            'actual': actual_direction,
            'signal': signal_dir,
            'confidence': confidence,
            'entry_up_ask': entry_up_ask,
            'entry_down_ask': entry_down_ask,
            'up_trajectory': up_trajectory,
            'down_trajectory': down_trajectory,
            'n_preds': len(probs),
        })
    
    return pd.DataFrame([{k:v for k,v in s.items() if k not in ['up_trajectory','down_trajectory']} for s in signals]), signals

# ========================================================================
# BACKTEST ENGINE
# ========================================================================

def backtest_hold_to_resolve(signals, bankroll, bet_frac, slippage, fee_rate, min_conf):
    """
    Strategy A: Buy contract and hold to resolution.
    If correct → payout $1.00 per share
    If wrong   → payout $0.00 per share
    """
    log = []
    equity_curve = [(0, bankroll)]
    
    for s in signals:
        if s['confidence'] < min_conf:
            continue
        
        # Which contract to buy
        side = s['signal']
        if side == 'UP':
            entry_ask = s['entry_up_ask']
        else:
            entry_ask = s['entry_down_ask']
        
        if entry_ask is None or entry_ask <= 0 or entry_ask >= 1:
            continue
        
        # Entry price with slippage and fees
        entry_price = entry_ask + slippage
        entry_price = min(entry_price, 0.99)  # Can't pay more than 0.99
        
        # Position sizing
        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price
        
        # Resolution
        correct = (side == s['actual'])
        if correct:
            payout = shares * 1.00  # Resolves to $1.00
        else:
            payout = shares * 0.00  # Resolves to $0.00
        
        # Exit fee on payout
        fee_exit = payout * fee_rate
        net_payout = payout - fee_exit
        
        # P&L
        pnl = net_payout - bet_amount
        bankroll += pnl
        
        start_dt = pd.to_datetime(s['start_ms'], unit='ms')
        end_dt = pd.to_datetime(s['end_ms'], unit='ms')
        
        log.append({
            'market': s['slug'],
            'entry_time': str(start_dt),
            'exit_time': str(end_dt),
            'side': side,
            'entry_price': round(entry_price, 4),
            'exit_price': 1.00 if correct else 0.00,
            'shares': round(shares, 2),
            'bet_amount': round(bet_amount, 2),
            'pnl': round(pnl, 2),
            'bankroll': round(bankroll, 2),
            'confidence': round(s['confidence'], 4),
            'actual': s['actual'],
            'correct': correct,
            'strategy': 'HOLD_TO_RESOLVE',
        })
        
        equity_curve.append((len(log), bankroll))
    
    return log, equity_curve, bankroll

def backtest_momentum(signals, bankroll, bet_frac, slippage, fee_rate, min_conf, take_profit):
    """
    Strategy B: Buy contract, try to exit at take-profit target.
    If TP hit → sell at (entry + take_profit)
    If TP not hit → hold to resolution
    """
    log = []
    equity_curve = [(0, bankroll)]
    
    for s in signals:
        if s['confidence'] < min_conf:
            continue
        
        side = s['signal']
        if side == 'UP':
            entry_ask = s['entry_up_ask']
            trajectory = s['up_trajectory']
        else:
            entry_ask = s['entry_down_ask']
            trajectory = s['down_trajectory']
        
        if entry_ask is None or entry_ask <= 0 or entry_ask >= 1:
            continue
        
        entry_price = entry_ask + slippage
        entry_price = min(entry_price, 0.99)
        
        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price
        
        # Check trajectory for take-profit
        tp_price = entry_price + take_profit
        exit_price = None
        exit_time = None
        exit_type = 'RESOLVE'
        
        if len(trajectory) > 0 and tp_price < 0.99:
            # Find first point where bid >= tp_price (we can sell)
            hits = trajectory[trajectory['best_bid'] >= tp_price]
            if len(hits) > 0:
                exit_price = hits.iloc[0]['best_bid'] - slippage
                exit_time = pd.to_datetime(hits.iloc[0]['source_ts_ms'], unit='ms')
                exit_type = 'TAKE_PROFIT'
        
        if exit_price is None:
            # Hold to resolution
            correct = (side == s['actual'])
            exit_price = 1.00 if correct else 0.00
            exit_time = pd.to_datetime(s['end_ms'], unit='ms')
            exit_type = 'RESOLVE_WIN' if correct else 'RESOLVE_LOSS'
        
        payout = shares * exit_price
        fee_exit = payout * fee_rate if payout > 0 else 0
        net_payout = payout - fee_exit
        pnl = net_payout - bet_amount
        bankroll += pnl
        
        correct = (side == s['actual'])
        start_dt = pd.to_datetime(s['start_ms'], unit='ms')
        
        log.append({
            'market': s['slug'],
            'entry_time': str(start_dt),
            'exit_time': str(exit_time),
            'side': side,
            'entry_price': round(entry_price, 4),
            'exit_price': round(exit_price, 4),
            'shares': round(shares, 2),
            'bet_amount': round(bet_amount, 2),
            'pnl': round(pnl, 2),
            'bankroll': round(bankroll, 2),
            'confidence': round(s['confidence'], 4),
            'actual': s['actual'],
            'correct': correct,
            'exit_type': exit_type,
            'take_profit_target': take_profit,
            'strategy': f'MOMENTUM_TP{int(take_profit*100)}',
        })
        
        equity_curve.append((len(log), bankroll))
    
    return log, equity_curve, bankroll

# ========================================================================
# REPORTING
# ========================================================================

def print_strategy_report(name, log, final_bankroll):
    if len(log) == 0:
        print(f"\n  {name}: No trades executed")
        return
    
    trades = len(log)
    wins = sum(1 for t in log if t['pnl'] > 0)
    losses = sum(1 for t in log if t['pnl'] <= 0)
    total_pnl = sum(t['pnl'] for t in log)
    avg_pnl = total_pnl / trades
    win_rate = wins / trades
    
    # Max drawdown
    peak = INITIAL_BANKROLL
    max_dd = 0
    equity = INITIAL_BANKROLL
    for t in log:
        equity += t['pnl']
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)
    
    roi = (final_bankroll - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
    
    print(f"\n{'-'*60}")
    print(f"  {name}")
    print(f"{'-'*60}")
    print(f"  Trades:       {trades}")
    print(f"  Wins:         {wins} ({win_rate:.1%})")
    print(f"  Losses:       {losses}")
    print(f"  Total P&L:    ${total_pnl:+.2f}")
    print(f"  Avg P&L:      ${avg_pnl:+.2f}")
    print(f"  Final Bank:   ${final_bankroll:.2f}")
    print(f"  ROI:          {roi:+.1f}%")
    print(f"  Max Drawdown: {max_dd:.1%}")
    
    # Best/worst trade
    best = max(log, key=lambda t: t['pnl'])
    worst = min(log, key=lambda t: t['pnl'])
    print(f"  Best Trade:   ${best['pnl']:+.2f} ({best['market']} {best['side']})")
    print(f"  Worst Trade:  ${worst['pnl']:+.2f} ({worst['market']} {worst['side']})")

def generate_chart(all_results):
    """Generate an HTML chart for equity curves."""
    # Build simple JS chart
    chart_data = {}
    for name, (log, ec, final) in all_results.items():
        curve_points = []
        bankroll = INITIAL_BANKROLL
        for trade in log:
            bankroll += trade['pnl']
            curve_points.append(bankroll)
        chart_data[name] = curve_points
    
    # Color palette
    colors = ['#00d4aa', '#ff6b6b', '#4ecdc4', '#ffd93d', '#6c5ce7', '#fd79a8', '#a29bfe']
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket BTC Backtest Results</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ 
    background: #0a0a1a; color: #e0e0e0; font-family: 'Inter', sans-serif;
    padding: 24px;
  }}
  h1 {{ 
    font-size: 28px; font-weight: 700; margin-bottom: 8px;
    background: linear-gradient(135deg, #00d4aa, #4ecdc4);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 32px; }}
  .chart-container {{
    background: #12122a; border-radius: 16px; padding: 24px;
    border: 1px solid #1e1e3a; margin-bottom: 24px;
  }}
  canvas {{ width: 100% !important; height: 400px !important; }}
  .stats-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .stat-card {{
    background: #12122a; border-radius: 12px; padding: 20px;
    border: 1px solid #1e1e3a;
  }}
  .stat-card h3 {{ font-size: 14px; color: #888; margin-bottom: 8px; font-weight: 500; }}
  .stat-card .value {{ font-size: 24px; font-weight: 700; }}
  .stat-card .value.positive {{ color: #00d4aa; }}
  .stat-card .value.negative {{ color: #ff6b6b; }}
  .trade-log {{
    background: #12122a; border-radius: 16px; padding: 24px;
    border: 1px solid #1e1e3a; overflow-x: auto;
  }}
  .trade-log h2 {{ font-size: 18px; margin-bottom: 16px; color: #fff; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; padding: 10px 8px; color: #888; border-bottom: 1px solid #1e1e3a; font-weight: 500; }}
  td {{ padding: 8px; border-bottom: 1px solid #0d0d22; }}
  tr:hover {{ background: #1a1a35; }}
  .win {{ color: #00d4aa; }}
  .loss {{ color: #ff6b6b; }}
  .legend {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 16px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>Polymarket BTC Backtest</h1>
<p class="subtitle">15-Minute Binary Options | ML Ensemble Signals | $100 Start | 5% Bet Size</p>
"""

    # Summary stats cards
    html += '<div class="stats-grid">'
    for i, (name, (log, ec, final)) in enumerate(all_results.items()):
        if len(log) == 0: continue
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100
        cls = 'positive' if roi >= 0 else 'negative'
        html += f'''<div class="stat-card">
            <h3>{name}</h3>
            <div class="value {cls}">${final:.2f} ({roi:+.1f}%)</div>
            <div style="color:#888;font-size:12px;margin-top:6px">{len(log)} trades | {wr:.0f}% win rate</div>
        </div>'''
    html += '</div>'

    # Chart
    html += '<div class="chart-container"><div class="legend">'
    for i, name in enumerate(chart_data.keys()):
        c = colors[i % len(colors)]
        html += f'<div class="legend-item"><div class="legend-dot" style="background:{c}"></div>{name}</div>'
    html += '</div><canvas id="chart"></canvas></div>'

    # Trade log for best strategy
    best_name = max(all_results.keys(), key=lambda k: all_results[k][2])
    best_log = all_results[best_name][0]
    
    html += f'<div class="trade-log"><h2>Trade Log — {best_name}</h2><table><thead><tr>'
    for h in ['#','Market','Time','Side','Entry','Exit','Shares','Bet','P&L','Bankroll','Conf','Actual','Result']:
        html += f'<th>{h}</th>'
    html += '</tr></thead><tbody>'
    
    for i, t in enumerate(best_log):
        cls = 'win' if t['pnl'] > 0 else 'loss'
        result = '✅' if t.get('correct') else '❌'
        exit_note = t.get('exit_type', '')
        html += f'<tr><td>{i+1}</td>'
        html += f'<td>{t["market"].split("-")[-1]}</td>'
        html += f'<td>{t["entry_time"][11:19]}</td>'
        html += f'<td>{t["side"]}</td>'
        html += f'<td>${t["entry_price"]:.3f}</td>'
        html += f'<td>${t["exit_price"]:.3f}</td>'
        html += f'<td>{t["shares"]:.1f}</td>'
        html += f'<td>${t["bet_amount"]:.2f}</td>'
        html += f'<td class="{cls}">${t["pnl"]:+.2f}</td>'
        html += f'<td>${t["bankroll"]:.2f}</td>'
        html += f'<td>{t["confidence"]:.2f}</td>'
        html += f'<td>{t["actual"]}</td>'
        html += f'<td>{result} {exit_note}</td>'
        html += '</tr>'
    
    html += '</tbody></table></div>'

    # Chart JS
    html += '<script>'
    html += 'const canvas = document.getElementById("chart");'
    html += 'const ctx = canvas.getContext("2d");'
    html += 'const dpr = window.devicePixelRatio || 1;'
    html += 'canvas.width = canvas.offsetWidth * dpr;'
    html += 'canvas.height = 400 * dpr;'
    html += 'ctx.scale(dpr, dpr);'
    html += 'const W = canvas.offsetWidth, H = 400;'
    
    # Serialize chart data as JSON and render entirely in JS
    import json as _json
    chart_json = _json.dumps(chart_data)
    color_json = _json.dumps(colors)
    html += f'const data = {chart_json};'
    html += f'const colorList = {color_json};'
    html += f'const initBank = {INITIAL_BANKROLL};'
    html += '''
    // Compute bounds
    let allVals = [initBank];
    for (let k of Object.keys(data)) { allVals.push(initBank); data[k].forEach(v => allVals.push(v)); }
    const minV = Math.min(...allVals) * 0.95;
    const maxV = Math.max(...allVals) * 1.05;
    
    // Grid
    ctx.strokeStyle = "#1e1e3a"; ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
      let y = H - 40 - (i/5)*(H-60);
      ctx.beginPath(); ctx.moveTo(50,y); ctx.lineTo(W-20,y); ctx.stroke();
      let val = minV + (i/5)*(maxV-minV);
      ctx.fillStyle="#666"; ctx.font="11px Inter";
      ctx.fillText("$"+val.toFixed(0), 5, y+4);
    }
    
    // Draw lines
    let ci = 0;
    for (let [name, pts] of Object.entries(data)) {
      let full = [initBank, ...pts];
      let c = colorList[ci % colorList.length]; ci++;
      ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.beginPath();
      for (let j = 0; j < full.length; j++) {
        let x = 50 + (j / Math.max(full.length-1, 1)) * (W - 70);
        let y = H - 40 - ((full[j] - minV) / (maxV - minV)) * (H - 60);
        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    '''
    
    # X axis label
    html += 'ctx.fillStyle="#888"; ctx.font="12px Inter";'
    html += f'ctx.fillText("Trade #", W/2-20, H-5);'
    
    html += '</script></body></html>'
    
    out_path = os.path.join(os.path.dirname(__file__), 'backtest_results_v1.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  📊 Chart saved to {out_path}")

# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()
    
    print("="*70)
    print(" POLYMARKET BTC 15-MIN COMPREHENSIVE BACKTEST")
    print("="*70)
    print(f"  Initial bankroll: ${INITIAL_BANKROLL}")
    print(f"  Bet size: {BET_FRACTION*100:.0f}% of bankroll")
    print(f"  Slippage: ${SLIPPAGE}")
    print(f"  Fees: {FEE_RATE*100:.1f}% per leg")
    print(f"  Min confidence: {MIN_CONFIDENCE}")
    
    # Load data
    print("\n  Loading data...")
    df_meta, df_ticks, df_trades, df_lag = load_all_data()
    print(f"  Markets: {len(df_meta)}, Ticks: {len(df_ticks)}, Trades: {len(df_trades)}")
    
    # Build signals
    signals_df, signals_full = build_market_signals(df_meta, df_trades, df_ticks, df_lag)
    print(f"\n  Markets with signals: {len(signals_df)}")
    
    if len(signals_df) > 0:
        correct = (signals_df['signal'] == signals_df['actual']).sum()
        print(f"  Signal accuracy: {correct}/{len(signals_df)} = {correct/len(signals_df):.1%}")
        print(f"  Signals: {signals_df['signal'].value_counts().to_dict()}")
    
    # ============================================================
    # Run all strategies
    # ============================================================
    all_results = {}
    
    # Strategy A: Hold to resolve
    print(f"\n{'='*70}")
    print(f" STRATEGY A: HOLD TO RESOLVE")
    print(f"{'='*70}")
    
    for conf_thresh in [0.50, 0.55, 0.60]:
        name = f'Hold (conf>{conf_thresh:.0%})'
        log, ec, final = backtest_hold_to_resolve(
            signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, conf_thresh)
        all_results[name] = (log, ec, final)
        print_strategy_report(name, log, final)
    
    # Strategy B: Momentum capture
    print(f"\n{'='*70}")
    print(f" STRATEGY B: MOMENTUM CAPTURE")
    print(f"{'='*70}")
    
    for tp in MOMENTUM_TARGETS:
        name = f'Momentum TP={tp:.0%}'
        log, ec, final = backtest_momentum(
            signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, MIN_CONFIDENCE, tp)
        all_results[name] = (log, ec, final)
        print_strategy_report(name, log, final)
    
    # ============================================================
    # Final comparison
    # ============================================================
    print(f"\n\n{'='*70}")
    print(f" STRATEGY COMPARISON")
    print(f"{'='*70}")
    
    for name, (log, ec, final) in sorted(all_results.items(), key=lambda x: -x[1][2]):
        if len(log) == 0:
            continue
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100 if len(log) > 0 else 0
        bar = '█' * max(1, int(roi / 5)) if roi > 0 else '░' * max(1, int(abs(roi) / 5))
        marker = '🏆' if name == max(all_results, key=lambda k: all_results[k][2]) else ''
        print(f"  {name:30s}: ${final:7.2f} ({roi:+6.1f}%)  WR={wr:4.0f}%  {bar} {marker}")
    
    # Generate chart
    generate_chart(all_results)
    
    # Save trade log
    best_name = max(all_results, key=lambda k: all_results[k][2])
    best_log = all_results[best_name][0]
    if best_log:
        df_log = pd.DataFrame(best_log)
        df_log.to_csv('trade_log.csv', index=False)
        print(f"  📋 Trade log saved to trade_log.csv")
    
    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.0f}s")

if __name__ == "__main__":
    main()
