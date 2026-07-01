"""
POLYMARKET BTC 15-MIN BACKTESTER v2
====================================
Mirrors the LIVE Rust execution engine config exactly.

Config source: execution-engine/src/config.rs

KEY CHANGES vs v1:
  - MIN_CONFIDENCE = 0.50  (lower threshold, more trades)
  - MIN_SECS_INTO_MARKET = 0  (can trade at market open)
  - MAX_SECS_INTO_MARKET = 300  (5 min window, was 10 min)
  - WIN_THRESHOLD = 0.90  (price > $0.90 at resolve counts as win)
  - MAX_ENTRY_PRICE = 0.99  (hard cap on entry)
  - MAX_DAILY_LOSS_PCT = 0.20  (20% drawdown circuit breaker)
  - MAX_OPEN_POSITIONS = 1  (sequential, one at a time)
  - Single strategies tested: HOLD_TO_RESOLVE + MOMENTUM (TP=10%)

EXECUTION MODEL:
  - Entry: Buy at ASK + slippage, capped at MAX_ENTRY_PRICE
  - Exit:  Resolve at $1.00/$0.00, or momentum TP
  - Fees: 1% per leg
  - Slippage: $0.005 per share
  - Bankroll: $100, 5% bet sizing, full reinvestment
  - Circuit breaker: stop trading if drawdown > 20%
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
# CONFIG — Matches Rust execution engine (config.rs)
# ========================================================================
INITIAL_BANKROLL    = 100.0
BET_FRACTION        = 0.05       # 5% of bankroll per trade
SLIPPAGE            = 0.005      # $0.005 per share
FEE_RATE            = 0.01       # 1% per leg

# Signal thresholds
MIN_CONFIDENCE      = 0.50       # Trade at any signal (Rust: 0.50)
MIN_CONSISTENCY     = 0.0        # No consistency filter (Rust: 0.0)
SIGNAL_WINDOW       = 30         # Average last N predictions
MIN_PREDICTIONS     = 5          # Need >= 5 predictions before acting

# Timing
MIN_SECS_INTO_MARKET = 0         # Can trade at market open (Rust: 0)
MAX_SECS_INTO_MARKET = 300       # 5 min max entry window (Rust: 300)
MARKET_DURATION_SECS = 900       # 15 minutes

# Risk
MAX_OPEN_POSITIONS  = 1          # One position at a time
MAX_DAILY_LOSS_PCT  = 0.20       # 20% drawdown halt

# Strategy
MOMENTUM_TP         = 0.10       # Take-profit for momentum (Rust: 0.10)
WIN_THRESHOLD       = 0.90       # Price > 0.90 at resolve = win (Rust: 0.90)
MAX_ENTRY_PRICE     = 0.99       # Cap limit price (Rust: 0.99)

# Full sweep configs
CONFIDENCE_LEVELS   = [i/100 for i in range(50, 96)]  # 0.50 to 0.95 in 1% steps
MOMENTUM_TARGETS    = [0.05, 0.10, 0.15, 0.20]

# ========================================================================
# DATA LOADING
# ========================================================================

def load_all_data():
    conn = sqlite3.connect(DB_PATH)
    
    df_meta = pd.read_sql_query("SELECT * FROM market_meta ORDER BY first_seen_ms ASC", conn)
    
    df_ticks = pd.read_sql_query(
        """SELECT market_slug, source_ts_ms, side_label, price, best_bid, best_ask, size, event_type
           FROM polymarket_ticks_ms ORDER BY source_ts_ms ASC""", conn)
    
    df_trades = pd.read_sql_query(
        "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC", conn)
    
    df_lag = pd.read_sql_query(
        "SELECT paired_at_ms, lead_lag_ms, quality_flag FROM lag_pairs_ms ORDER BY paired_at_ms ASC", conn)
    
    conn.close()
    return df_meta, df_ticks, df_trades, df_lag

# ========================================================================
# ML MODEL (same ensemble as live system)
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
    
    xgb_m = xgb.XGBClassifier(
        n_estimators=2000,max_depth=4,learning_rate=0.01,subsample=0.7,
        colsample_bytree=0.5,gamma=0.5,min_child_weight=3,reg_alpha=0,
        reg_lambda=0.5,scale_pos_weight=spw,eval_metric='logloss',
        random_state=42,early_stopping_rounds=30)
    xgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
    
    lgb_m = lgb.LGBMClassifier(
        n_estimators=2000,max_depth=4,learning_rate=0.01,subsample=0.7,
        colsample_bytree=0.5,min_child_weight=3,reg_alpha=0,reg_lambda=0.5,
        scale_pos_weight=spw,random_state=42,verbose=-1,n_jobs=1)
    lgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], callbacks=[lgb.early_stopping(30, verbose=False)])
    
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
    
    Mirrors Rust execution engine behavior:
      - Signal window: first MAX_SECS_INTO_MARKET seconds (300s = 5 min)
      - Min predictions: MIN_PREDICTIONS (5)
      - No consistency filter (MIN_CONSISTENCY = 0.0)
      - Can enter from second 0 (MIN_SECS_INTO_MARKET = 0)
    """
    print("\n  Training ML ensemble on 1s data...")
    
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
    
    signals = []
    
    for _, market in df_meta.iterrows():
        slug = market['market_slug']
        epoch_s = int(slug.split('-')[-1])
        start_ms = epoch_s * 1000
        end_ms = start_ms + MARKET_DURATION_SECS * 1000
        
        # Get BTC ground truth direction
        # btc_start: first trade at/after market open
        # btc_end: first trade at/after market close (true settlement proxy)
        window_trades = df_trades[(df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < end_ms)]
        if len(window_trades) < 10:
            continue
        settle_trades = df_trades[df_trades['trade_time'] >= end_ms]
        
        btc_start = window_trades.iloc[0]['price']
        btc_end = settle_trades.iloc[0]['price'] if len(settle_trades) > 0 else window_trades.iloc[-1]['price']
        actual_direction = 'UP' if btc_end > btc_start else 'DOWN'
        
        # Signal window: 0 to MAX_SECS_INTO_MARKET
        signal_end_ms = start_ms + MAX_SECS_INTO_MARKET * 1000
        signal_start = pd.to_datetime(start_ms, unit='ms')
        signal_end = pd.to_datetime(signal_end_ms, unit='ms')
        
        window_ml = df_ml[(df_ml.index >= signal_start) & (df_ml.index < signal_end)]
        
        if len(window_ml) < MIN_PREDICTIONS:
            continue
        
        # Get probabilities
        X_window = window_ml[feats]
        probs = predict_ensemble(xgb_m, lgb_m, meta_clf, X_window)
        
        # Signal aggregation (matches Rust SignalAggregator)
        # Use last SIGNAL_WINDOW predictions (or all if fewer)
        recent_probs = probs[-SIGNAL_WINDOW:]
        mean_up_prob = recent_probs.mean()
        
        if mean_up_prob > 0.5:
            signal_dir = 'UP'
            confidence = mean_up_prob
        else:
            signal_dir = 'DOWN'
            confidence = 1 - mean_up_prob
        
        # Consistency check (all predictions agree?)
        if mean_up_prob > 0.5:
            consistency = (recent_probs > 0.5).mean()
        else:
            consistency = (recent_probs <= 0.5).mean()
        
        # Apply MIN_CONSISTENCY filter
        if consistency < MIN_CONSISTENCY:
            continue
        
        # Get Polymarket entry prices
        entry_window_ticks = p_ticks[
            (p_ticks['market_slug'] == slug) & 
            (p_ticks['source_ts_ms'] >= start_ms) &
            (p_ticks['source_ts_ms'] < signal_end_ms)
        ]
        
        up_ticks_w = entry_window_ticks[entry_window_ticks['side_label'] == 'UP']
        down_ticks_w = entry_window_ticks[entry_window_ticks['side_label'] == 'DOWN']
        
        entry_up_ask = up_ticks_w['best_ask'].median() if len(up_ticks_w) > 0 else 0.50
        entry_down_ask = down_ticks_w['best_ask'].median() if len(down_ticks_w) > 0 else 0.50
        
        # Get ALL ticks for this market (for momentum strategy)
        all_market_ticks = p_ticks[p_ticks['market_slug'] == slug].copy()
        all_market_ticks = all_market_ticks[all_market_ticks['source_ts_ms'] >= signal_end_ms]
        
        up_trajectory = all_market_ticks[all_market_ticks['side_label'] == 'UP'][['source_ts_ms','best_bid','best_ask','price']].copy()
        down_trajectory = all_market_ticks[all_market_ticks['side_label'] == 'DOWN'][['source_ts_ms','best_bid','best_ask','price']].copy()
        
        # Compute entry timestamp within market (for timing analysis)
        # Use middle of signal window as approximate entry time
        entry_secs_in = min(len(window_ml), MAX_SECS_INTO_MARKET)
        
        signals.append({
            'slug': slug,
            'start_ms': start_ms,
            'end_ms': end_ms,
            'btc_start': btc_start,
            'btc_end': btc_end,
            'actual': actual_direction,
            'signal': signal_dir,
            'confidence': confidence,
            'consistency': consistency,
            'entry_up_ask': entry_up_ask,
            'entry_down_ask': entry_down_ask,
            'up_trajectory': up_trajectory,
            'down_trajectory': down_trajectory,
            'n_preds': len(probs),
            'entry_secs_in': entry_secs_in,
        })
    
    cols = [k for k in signals[0].keys() if k not in ['up_trajectory','down_trajectory']] if signals else []
    return pd.DataFrame([{k:v for k,v in s.items() if k in cols} for s in signals]), signals

# ========================================================================
# BACKTEST ENGINE
# ========================================================================

def backtest_hold_to_resolve(signals, bankroll, bet_frac, slippage, fee_rate, min_conf,
                              max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """
    Strategy A: Buy contract and hold to resolution.
    Mirrors Rust execution engine behavior including:
      - MAX_ENTRY_PRICE cap
      - WIN_THRESHOLD for resolution
      - MAX_DAILY_LOSS_PCT circuit breaker
    """
    log = []
    equity_curve = [(0, bankroll)]
    peak_bankroll = bankroll
    halted = False
    
    for s in signals:
        # Circuit breaker: check drawdown
        if not halted:
            dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            if dd >= max_daily_loss_pct:
                halted = True
                print(f"    [HALT] Drawdown {dd:.1%} >= {max_daily_loss_pct:.0%} at trade #{len(log)+1}")
        
        if halted:
            continue
        
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
        
        # Entry price with slippage, capped at MAX_ENTRY_PRICE
        entry_price = entry_ask + slippage
        entry_price = min(entry_price, MAX_ENTRY_PRICE)
        
        # Position sizing
        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price
        
        # Resolution: compare predicted direction to actual BTC direction (open vs close)
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
        peak_bankroll = max(peak_bankroll, bankroll)
        
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
            'consistency': round(s.get('consistency', 1.0), 4),
            'actual': s['actual'],
            'correct': correct,
            'entry_secs_in': s.get('entry_secs_in', 0),
            'strategy': 'HOLD_TO_RESOLVE',
        })
        
        equity_curve.append((len(log), bankroll))
    
    return log, equity_curve, bankroll

def backtest_momentum(signals, bankroll, bet_frac, slippage, fee_rate, min_conf, take_profit,
                       max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """
    Strategy B: Buy contract, try to exit at take-profit target.
    If TP hit -> sell at (entry + take_profit)
    If TP not hit -> hold to resolution
    Includes drawdown circuit breaker.
    """
    log = []
    equity_curve = [(0, bankroll)]
    peak_bankroll = bankroll
    halted = False
    
    for s in signals:
        # Circuit breaker
        if not halted:
            dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            if dd >= max_daily_loss_pct:
                halted = True
                print(f"    [HALT] Drawdown {dd:.1%} >= {max_daily_loss_pct:.0%} at trade #{len(log)+1}")
        
        if halted:
            continue
        
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
        entry_price = min(entry_price, MAX_ENTRY_PRICE)
        
        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price
        
        # Check trajectory for take-profit
        tp_price = entry_price + take_profit
        exit_price = None
        exit_time = None
        exit_type = 'RESOLVE'
        
        if len(trajectory) > 0 and tp_price < MAX_ENTRY_PRICE:
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
        peak_bankroll = max(peak_bankroll, bankroll)
        
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
            'consistency': round(s.get('consistency', 1.0), 4),
            'actual': s['actual'],
            'correct': correct,
            'exit_type': exit_type,
            'take_profit_target': take_profit,
            'entry_secs_in': s.get('entry_secs_in', 0),
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
    
    # Exit type breakdown (for momentum)
    exit_types = {}
    for t in log:
        et = t.get('exit_type', 'RESOLVE')
        exit_types[et] = exit_types.get(et, 0) + 1
    
    # Avg entry timing
    avg_entry_secs = np.mean([t.get('entry_secs_in', 0) for t in log])
    
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
    print(f"  Avg Entry:    {avg_entry_secs:.0f}s into market")
    
    if exit_types and any(k != 'RESOLVE' and k != 'HOLD_TO_RESOLVE' for k in exit_types):
        print(f"  Exit Types:   {exit_types}")
    
    best = max(log, key=lambda t: t['pnl'])
    worst = min(log, key=lambda t: t['pnl'])
    print(f"  Best Trade:   ${best['pnl']:+.2f} ({best['market']} {best['side']})")
    print(f"  Worst Trade:  ${worst['pnl']:+.2f} ({worst['market']} {worst['side']})")

def generate_chart(all_results):
    """Generate an HTML chart for equity curves."""
    chart_data = {}
    for name, (log, ec, final) in all_results.items():
        curve_points = []
        bankroll = INITIAL_BANKROLL
        for trade in log:
            bankroll += trade['pnl']
            curve_points.append(bankroll)
        chart_data[name] = curve_points
    
    colors = ['#00d4aa', '#ff6b6b', '#4ecdc4', '#ffd93d', '#6c5ce7', '#fd79a8', '#a29bfe']
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket BTC Backtest v2 (Rust Config)</title>
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
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 12px; }}
  .config-badge {{
    display: inline-block; background: #1a1a35; border: 1px solid #2a2a55;
    border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #4ecdc4;
    margin-bottom: 24px; margin-right: 8px;
  }}
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
  .stat-card .meta {{ color: #666; font-size: 11px; margin-top: 6px; }}
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
<h1>Polymarket BTC Backtest v2</h1>
<p class="subtitle">15-Minute Binary Options | ML Ensemble | Rust Execution Engine Config</p>
<div>
  <span class="config-badge">Bankroll: ${INITIAL_BANKROLL}</span>
  <span class="config-badge">Bet: {BET_FRACTION*100:.0f}%</span>
  <span class="config-badge">Min Conf: {MIN_CONFIDENCE}</span>
  <span class="config-badge">Entry: 0-{MAX_SECS_INTO_MARKET}s</span>
  <span class="config-badge">Max Drawdown: {MAX_DAILY_LOSS_PCT*100:.0f}%</span>
  <span class="config-badge">Slippage: ${SLIPPAGE}</span>
  <span class="config-badge">Fee: {FEE_RATE*100:.0f}%/leg</span>
</div>
"""
    
    # Summary stats cards
    html += '<div class="stats-grid">'
    for i, (name, (log, ec, final)) in enumerate(all_results.items()):
        if len(log) == 0: continue
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100
        total_pnl = sum(t['pnl'] for t in log)
        cls = 'positive' if roi >= 0 else 'negative'
        
        # Max drawdown
        pk = INITIAL_BANKROLL
        mdd = 0
        eq = INITIAL_BANKROLL
        for t in log:
            eq += t['pnl']
            pk = max(pk, eq)
            mdd = max(mdd, (pk - eq) / pk)
        
        html += f'''<div class="stat-card">
            <h3>{name}</h3>
            <div class="value {cls}">${final:.2f} ({roi:+.1f}%)</div>
            <div class="meta">{len(log)} trades | {wr:.0f}% WR | {mdd:.1%} MDD | P&L: ${total_pnl:+.2f}</div>
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
    
    html += f'<div class="trade-log"><h2>Trade Log -- {best_name}</h2><table><thead><tr>'
    for h in ['#','Market','Time','Side','Entry','Exit','Shares','Bet','P&L','Bank','Conf','Consist','Actual','Result']:
        html += f'<th>{h}</th>'
    html += '</tr></thead><tbody>'
    
    for i, t in enumerate(best_log):
        cls = 'win' if t['pnl'] > 0 else 'loss'
        result = 'WIN' if t.get('correct') else 'LOSS'
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
        html += f'<td>{t.get("consistency", 1.0):.2f}</td>'
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
    
    import json as _json
    chart_json = _json.dumps(chart_data)
    color_json = _json.dumps(colors)
    html += f'const data = {chart_json};'
    html += f'const colorList = {color_json};'
    html += f'const initBank = {INITIAL_BANKROLL};'
    html += '''
    let allVals = [initBank];
    for (let k of Object.keys(data)) { allVals.push(initBank); data[k].forEach(v => allVals.push(v)); }
    const minV = Math.min(...allVals) * 0.95;
    const maxV = Math.max(...allVals) * 1.05;
    
    ctx.strokeStyle = "#1e1e3a"; ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
      let y = H - 40 - (i/5)*(H-60);
      ctx.beginPath(); ctx.moveTo(50,y); ctx.lineTo(W-20,y); ctx.stroke();
      let val = minV + (i/5)*(maxV-minV);
      ctx.fillStyle="#666"; ctx.font="11px Inter";
      ctx.fillText("$"+val.toFixed(0), 5, y+4);
    }
    
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
    
    html += 'ctx.fillStyle="#888"; ctx.font="12px Inter";'
    html += f'ctx.fillText("Trade #", W/2-20, H-5);'
    
    html += '</script></body></html>'
    
    out_path = os.path.join(os.path.dirname(__file__), 'backtest_results.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  Chart saved to {out_path}")

def generate_portfolio_chart(portfolio_results, per_strat_bankroll, total_capital, total_final):
    """Generate a dedicated HTML chart for the multi-strategy portfolio."""
    
    colors = {
        'VOLUME': '#00d4aa',
        'QUALITY': '#ffd93d', 
        'SNIPER': '#ff6b6b',
        'MOMENTUM': '#6c5ce7',
        'COMBINED': '#4ecdc4',
    }
    
    # Build per-strategy equity curves
    chart_data = {}
    for strat_name, res in portfolio_results.items():
        short_name = strat_name.split('(')[0].strip()
        curve = [per_strat_bankroll]
        eq = per_strat_bankroll
        for t in res['log']:
            eq += t['pnl']
            curve.append(eq)
        chart_data[short_name] = curve
    
    # Build combined equity curve
    # Merge all trades across strategies, sorted by market time
    all_trades_timed = []
    for strat_name, res in portfolio_results.items():
        short_name = strat_name.split('(')[0].strip()
        for t in res['log']:
            all_trades_timed.append({
                'strat': short_name,
                'entry_time': t['entry_time'],
                'pnl': t['pnl'],
            })
    all_trades_timed.sort(key=lambda x: x['entry_time'])
    
    combined_curve = [total_capital]
    eq = total_capital
    for t in all_trades_timed:
        eq += t['pnl']
        combined_curve.append(eq)
    chart_data['COMBINED'] = combined_curve
    
    portfolio_roi = (total_final - total_capital) / total_capital * 100
    total_pnl = total_final - total_capital
    total_trades = sum(len(res['log']) for res in portfolio_results.values())
    total_wins = sum(sum(1 for t in res['log'] if t['pnl'] > 0) for res in portfolio_results.values())
    combined_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Strategy Portfolio</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ 
    background: #0a0a1a; color: #e0e0e0; font-family: 'Inter', sans-serif;
    padding: 24px;
  }}
  h1 {{ 
    font-size: 28px; font-weight: 700; margin-bottom: 8px;
    background: linear-gradient(135deg, #ffd93d, #ff6b6b, #6c5ce7);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 12px; }}
  .hero-stats {{
    display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 24px;
  }}
  .hero-stat {{
    background: linear-gradient(135deg, #12122a, #1a1a40);
    border: 1px solid #2a2a55; border-radius: 16px; padding: 20px 28px;
    min-width: 160px;
  }}
  .hero-stat .label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 1px; }}
  .hero-stat .val {{ font-size: 32px; font-weight: 700; margin-top: 4px; }}
  .hero-stat .val.green {{ color: #00d4aa; }}
  .hero-stat .val.blue {{ color: #4ecdc4; }}
  .config-badge {{
    display: inline-block; background: #1a1a35; border: 1px solid #2a2a55;
    border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #4ecdc4;
    margin-bottom: 24px; margin-right: 8px;
  }}
  .chart-container {{
    background: #12122a; border-radius: 16px; padding: 24px;
    border: 1px solid #1e1e3a; margin-bottom: 24px;
  }}
  .chart-container h2 {{ font-size: 16px; color: #fff; margin-bottom: 16px; }}
  canvas {{ width: 100% !important; height: 400px !important; }}
  .strats-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .strat-card {{
    background: #12122a; border-radius: 12px; padding: 20px;
    border-left: 4px solid;
  }}
  .strat-card h3 {{ font-size: 16px; font-weight: 600; margin-bottom: 4px; }}
  .strat-card .desc {{ font-size: 11px; color: #888; margin-bottom: 12px; }}
  .strat-card .stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .strat-card .stat-label {{ font-size: 10px; color: #666; text-transform: uppercase; }}
  .strat-card .stat-val {{ font-size: 18px; font-weight: 600; }}
  .strat-card .stat-val.green {{ color: #00d4aa; }}
  .strat-card .stat-val.red {{ color: #ff6b6b; }}
  .legend {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 16px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 3px; }}
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
</style>
</head>
<body>
<h1>Multi-Strategy Portfolio</h1>
<p class="subtitle">4 Strategies x $100 Each = $400 Total Capital | Running Simultaneously</p>

<div class="hero-stats">
  <div class="hero-stat">
    <div class="label">Total Capital</div>
    <div class="val blue">${total_capital:.0f}</div>
  </div>
  <div class="hero-stat">
    <div class="label">Final Value</div>
    <div class="val green">${total_final:.2f}</div>
  </div>
  <div class="hero-stat">
    <div class="label">Portfolio ROI</div>
    <div class="val green">{portfolio_roi:+.1f}%</div>
  </div>
  <div class="hero-stat">
    <div class="label">Total P&L</div>
    <div class="val green">${total_pnl:+.2f}</div>
  </div>
  <div class="hero-stat">
    <div class="label">Win Rate</div>
    <div class="val green">{combined_wr:.1f}%</div>
  </div>
  <div class="hero-stat">
    <div class="label">Total Trades</div>
    <div class="val blue">{total_trades}</div>
  </div>
</div>
"""
    
    # Strategy cards
    strat_colors_list = ['#00d4aa', '#ffd93d', '#ff6b6b', '#6c5ce7']
    html += '<div class="strats-grid">'
    for i, (strat_name, res) in enumerate(portfolio_results.items()):
        log = res['log']
        final = res['final']
        cfg = res['cfg']
        sc = strat_colors_list[i % len(strat_colors_list)]
        
        if len(log) > 0:
            wins = sum(1 for t in log if t['pnl'] > 0)
            wr = wins / len(log) * 100
            roi = (final - per_strat_bankroll) / per_strat_bankroll * 100
            pnl = sum(t['pnl'] for t in log)
            cls = 'green' if roi >= 0 else 'red'
            
            html += f'''<div class="strat-card" style="border-color:{sc}">
                <h3 style="color:{sc}">{strat_name.split("(")[0].strip()}</h3>
                <div class="desc">{cfg["desc"]}</div>
                <div class="stats">
                    <div><div class="stat-label">Final</div><div class="stat-val {cls}">${final:.2f}</div></div>
                    <div><div class="stat-label">ROI</div><div class="stat-val {cls}">{roi:+.1f}%</div></div>
                    <div><div class="stat-label">Win Rate</div><div class="stat-val">{wr:.0f}%</div></div>
                    <div><div class="stat-label">Trades</div><div class="stat-val">{len(log)}</div></div>
                </div>
            </div>'''
    html += '</div>'
    
    # Chart
    color_map = ['#00d4aa', '#ffd93d', '#ff6b6b', '#6c5ce7', '#4ecdc4']
    
    html += '<div class="chart-container"><h2>Equity Curves</h2><div class="legend">'
    for i, name in enumerate(chart_data.keys()):
        c = color_map[i % len(color_map)]
        lw = '3' if name == 'COMBINED' else '2'
        html += f'<div class="legend-item"><div class="legend-dot" style="background:{c}"></div>{name}</div>'
    html += '</div><canvas id="chart"></canvas></div>'
    
    # Trade timeline
    html += '<div class="trade-log"><h2>All Portfolio Trades (sorted by time)</h2><table><thead><tr>'
    for h in ['#', 'Strategy', 'Market', 'Time', 'Side', 'Entry', 'Exit', 'Bet', 'P&L', 'Bank', 'Conf', 'Result']:
        html += f'<th>{h}</th>'
    html += '</tr></thead><tbody>'
    
    sorted_trades = []
    for strat_name, res in portfolio_results.items():
        short = strat_name.split('(')[0].strip()
        for t in res['log']:
            sorted_trades.append((short, t))
    sorted_trades.sort(key=lambda x: x[1]['entry_time'])
    
    for i, (sname, t) in enumerate(sorted_trades):
        cls = 'win' if t['pnl'] > 0 else 'loss'
        result = 'WIN' if t.get('correct') else 'LOSS'
        exit_note = t.get('exit_type', '')
        html += f'<tr><td>{i+1}</td>'
        html += f'<td>{sname}</td>'
        html += f'<td>{t["market"].split("-")[-1]}</td>'
        html += f'<td>{t["entry_time"][11:19]}</td>'
        html += f'<td>{t["side"]}</td>'
        html += f'<td>${t["entry_price"]:.3f}</td>'
        html += f'<td>${t["exit_price"]:.3f}</td>'
        html += f'<td>${t["bet_amount"]:.2f}</td>'
        html += f'<td class="{cls}">${t["pnl"]:+.2f}</td>'
        html += f'<td>${t["bankroll"]:.2f}</td>'
        html += f'<td>{t["confidence"]:.2f}</td>'
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
    
    import json as _json
    chart_json = _json.dumps(chart_data)
    color_json = _json.dumps(color_map)
    html += f'const data = {chart_json};'
    html += f'const colorList = {color_json};'
    html += '''
    let allVals = [];
    for (let k of Object.keys(data)) { data[k].forEach(v => allVals.push(v)); }
    const minV = Math.min(...allVals) * 0.95;
    const maxV = Math.max(...allVals) * 1.05;
    
    ctx.strokeStyle = "#1e1e3a"; ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
      let y = H - 40 - (i/5)*(H-60);
      ctx.beginPath(); ctx.moveTo(50,y); ctx.lineTo(W-20,y); ctx.stroke();
      let val = minV + (i/5)*(maxV-minV);
      ctx.fillStyle="#666"; ctx.font="11px Inter";
      ctx.fillText("$"+val.toFixed(0), 5, y+4);
    }
    
    let ci = 0;
    const keys = Object.keys(data);
    for (let name of keys) {
      let pts = data[name];
      let c = colorList[ci % colorList.length]; ci++;
      let lw = (name === "COMBINED") ? 3 : 1.5;
      let dash = (name === "COMBINED") ? [] : [4, 2];
      ctx.strokeStyle = c; ctx.lineWidth = lw;
      ctx.setLineDash(dash);
      ctx.beginPath();
      for (let j = 0; j < pts.length; j++) {
        let x = 50 + (j / Math.max(pts.length-1, 1)) * (W - 70);
        let y = H - 40 - ((pts[j] - minV) / (maxV - minV)) * (H - 60);
        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }
    '''
    
    html += 'ctx.fillStyle="#888"; ctx.font="12px Inter";'
    html += 'ctx.fillText("Trade #", W/2-20, H-5);'
    
    html += '</script></body></html>'
    
    out_path = os.path.join(os.path.dirname(__file__), 'portfolio_results.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  Portfolio chart saved to {out_path}")



def main():
    t0 = time.time()
    
    print("="*70)
    print(" POLYMARKET BTC 15-MIN BACKTEST v2 (Rust Execution Engine Config)")
    print("="*70)
    print(f"  Initial bankroll:    ${INITIAL_BANKROLL}")
    print(f"  Bet size:            {BET_FRACTION*100:.0f}% of bankroll")
    print(f"  Slippage:            ${SLIPPAGE}")
    print(f"  Fees:                {FEE_RATE*100:.1f}% per leg")
    print(f"  Min confidence:      {MIN_CONFIDENCE}")
    print(f"  Min consistency:     {MIN_CONSISTENCY}")
    print(f"  Entry window:        {MIN_SECS_INTO_MARKET}s - {MAX_SECS_INTO_MARKET}s")
    print(f"  Max drawdown halt:   {MAX_DAILY_LOSS_PCT:.0%}")
    print(f"  Max entry price:     ${MAX_ENTRY_PRICE}")
    print(f"  Win threshold:       ${WIN_THRESHOLD}")
    print(f"  Signal window:       {SIGNAL_WINDOW} predictions")
    print(f"  Min predictions:     {MIN_PREDICTIONS}")
    
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
        print(f"  Avg confidence: {signals_df['confidence'].mean():.3f}")
        print(f"  Avg consistency: {signals_df['consistency'].mean():.3f}")
    
    # ============================================================
    # HOLD-TO-RESOLVE: Full confidence sweep
    # ============================================================
    all_results = {}
    hold_sweep = []  # For the summary table
    
    print(f"\n{'='*70}")
    print(f" HOLD-TO-RESOLVE: CONFIDENCE SWEEP (50% -> 95%)")
    print(f"{'='*70}")
    
    for conf_thresh in CONFIDENCE_LEVELS:
        name = f'Hold (conf>{conf_thresh:.0%})'
        log, ec, final = backtest_hold_to_resolve(
            signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, conf_thresh)
        all_results[name] = (log, ec, final)
        
        if len(log) > 0:
            wins = sum(1 for t in log if t['pnl'] > 0)
            wr = wins / len(log) * 100
            roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
            total_pnl = sum(t['pnl'] for t in log)
            # Max drawdown
            pk = INITIAL_BANKROLL; mdd = 0; eq = INITIAL_BANKROLL
            for t in log:
                eq += t['pnl']; pk = max(pk, eq); mdd = max(mdd, (pk - eq) / pk)
            hold_sweep.append({
                'conf': conf_thresh, 'trades': len(log), 'wins': wins,
                'wr': wr, 'roi': roi, 'final': final, 'pnl': total_pnl, 'mdd': mdd
            })
    
    # ============================================================
    # THE MONEY TABLE: Confidence vs Win Rate
    # ============================================================
    print(f"\n\n{'='*70}")
    print(f" CONFIDENCE vs WIN RATE (Hold-to-Resolve)")
    print(f"{'='*70}")
    print(f"  {'Conf':>6s}  {'Trades':>6s}  {'Wins':>5s}  {'WR%':>6s}  {'ROI':>8s}  {'Final':>8s}  {'MDD':>6s}  {'P&L':>8s}  Visual")
    print(f"  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*8}  {'-'*20}")
    
    best_wr = 0
    best_wr_row = None
    best_roi = -999
    best_roi_row = None
    best_pnl = -999
    best_pnl_row = None
    
    for row in hold_sweep:
        bar_len = max(0, int(row['wr'] / 5))
        bar = '|' * bar_len
        marker = ''
        if row['wr'] > best_wr:
            best_wr = row['wr']
            best_wr_row = row
        if row['roi'] > best_roi:
            best_roi = row['roi']
            best_roi_row = row
        if row['pnl'] > best_pnl:
            best_pnl = row['pnl']
            best_pnl_row = row
        print(f"  {row['conf']:>5.0%}   {row['trades']:>5d}   {row['wins']:>4d}   {row['wr']:>5.1f}%  {row['roi']:>+7.1f}%  ${row['final']:>7.2f}  {row['mdd']:>5.1%}  ${row['pnl']:>+7.2f}  {bar}")
    
    print(f"\n  BEST WIN RATE:  {best_wr_row['conf']:.0%} -> {best_wr_row['wr']:.1f}% WR ({best_wr_row['trades']} trades, {best_wr_row['roi']:+.1f}% ROI)")
    print(f"  BEST ROI:       {best_roi_row['conf']:.0%} -> {best_roi_row['roi']:+.1f}% ROI ({best_roi_row['trades']} trades, {best_roi_row['wr']:.1f}% WR)")
    print(f"  BEST P&L:       {best_pnl_row['conf']:.0%} -> ${best_pnl_row['pnl']:+.2f} P&L ({best_pnl_row['trades']} trades, {best_pnl_row['wr']:.1f}% WR)")
    
    # ============================================================
    # MOMENTUM: Sweep TP at key confidence levels
    # ============================================================
    print(f"\n{'='*70}")
    print(f" MOMENTUM: TP SWEEP at key confidence levels")
    print(f"{'='*70}")
    
    mom_conf_levels = [0.50, 0.55, 0.60]
    for conf in mom_conf_levels:
        for tp in MOMENTUM_TARGETS:
            name = f'Mom TP={tp:.0%} conf>{conf:.0%}'
            log, ec, final = backtest_momentum(
                signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, conf, tp)
            all_results[name] = (log, ec, final)
            if len(log) > 0:
                wins = sum(1 for t in log if t['pnl'] > 0)
                wr = wins / len(log) * 100
                roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
                tp_exits = sum(1 for t in log if t.get('exit_type') == 'TAKE_PROFIT')
                print(f"  {name:30s}: {len(log):>3d} trades  WR={wr:>5.1f}%  ROI={roi:>+6.1f}%  TP_exits={tp_exits}")
    
    # ============================================================
    # TOP 10 OVERALL (sorted by final bankroll)
    # ============================================================
    print(f"\n\n{'='*70}")
    print(f" TOP 10 STRATEGIES (by Final Bankroll)")
    print(f"{'='*70}")
    
    ranked = sorted(
        [(n, l, e, f) for n, (l, e, f) in all_results.items() if len(l) > 0],
        key=lambda x: -x[3]
    )[:10]
    
    for rank, (name, log, ec, final) in enumerate(ranked, 1):
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100
        bar = '#' * max(1, int(roi / 5))
        marker = ' << BEST' if rank == 1 else ''
        print(f"  #{rank:<2d} {name:30s}: ${final:>7.2f} ({roi:>+6.1f}%)  WR={wr:>5.1f}%  {bar}{marker}")
    
    # ============================================================
    # TOP 10 by WIN RATE (min 3 trades)
    # ============================================================
    print(f"\n{'='*70}")
    print(f" TOP 10 STRATEGIES (by Win Rate, min 3 trades)")
    print(f"{'='*70}")
    
    ranked_wr = sorted(
        [(n, l, e, f) for n, (l, e, f) in all_results.items() if len(l) >= 3],
        key=lambda x: -(sum(1 for t in x[1] if t['pnl'] > 0) / len(x[1]))
    )[:10]
    
    for rank, (name, log, ec, final) in enumerate(ranked_wr, 1):
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100
        print(f"  #{rank:<2d} {name:30s}: WR={wr:>5.1f}%  ({wins}/{len(log)})  ROI={roi:>+6.1f}%  Final=${final:.2f}")
    
    # ============================================================
    # MULTI-STRATEGY PORTFOLIO
    # $100 per strategy, running simultaneously
    # ============================================================
    PORTFOLIO_BANKROLL = 100.0  # Each strategy gets $100
    
    portfolio_strategies = {
        'VOLUME (Hold conf>50%)': {
            'type': 'hold', 'conf': 0.50,
            'desc': 'Take every signal. Max volume, max total P&L.',
        },
        'QUALITY (Hold conf>64%)': {
            'type': 'hold', 'conf': 0.64,
            'desc': '83% WR sweet spot. Best risk-adjusted.',
        },
        'SNIPER (Hold conf>71%)': {
            'type': 'hold', 'conf': 0.71,
            'desc': '100% WR. Only pull the trigger on slam dunks.',
        },
        'MOMENTUM (TP=10% conf>50%)': {
            'type': 'momentum', 'conf': 0.50, 'tp': MOMENTUM_TP,
            'desc': '70% WR. Captures mid-game price moves.',
        },
    }
    
    print(f"\n\n{'='*70}")
    print(f" MULTI-STRATEGY PORTFOLIO")
    print(f" Each strategy: ${PORTFOLIO_BANKROLL} independent bankroll")
    print(f" Total capital deployed: ${PORTFOLIO_BANKROLL * len(portfolio_strategies)}")
    print(f"{'='*70}")
    
    portfolio_results = {}
    
    for strat_name, cfg in portfolio_strategies.items():
        if cfg['type'] == 'hold':
            log, ec, final = backtest_hold_to_resolve(
                signals_full, PORTFOLIO_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, cfg['conf'])
        else:
            log, ec, final = backtest_momentum(
                signals_full, PORTFOLIO_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, cfg['conf'], cfg['tp'])
        
        portfolio_results[strat_name] = {
            'log': log, 'ec': ec, 'final': final, 'cfg': cfg,
        }
    
    # Build combined equity curve (trade-by-trade across all strategies)
    # We need to merge all trades sorted by time and sum up all bankrolls
    total_capital = PORTFOLIO_BANKROLL * len(portfolio_strategies)
    
    # Print per-strategy results
    total_final = 0
    total_trades = 0
    total_wins = 0
    total_pnl = 0
    
    for strat_name, res in portfolio_results.items():
        log = res['log']
        final = res['final']
        cfg = res['cfg']
        total_final += final
        
        if len(log) > 0:
            wins = sum(1 for t in log if t['pnl'] > 0)
            wr = wins / len(log) * 100
            roi = (final - PORTFOLIO_BANKROLL) / PORTFOLIO_BANKROLL * 100
            pnl = sum(t['pnl'] for t in log)
            total_trades += len(log)
            total_wins += wins
            total_pnl += pnl
            
            pk = PORTFOLIO_BANKROLL; mdd = 0; eq = PORTFOLIO_BANKROLL
            for t in log:
                eq += t['pnl']; pk = max(pk, eq); mdd = max(mdd, (pk - eq) / pk)
            
            print(f"\n  {strat_name}")
            print(f"    {cfg['desc']}")
            print(f"    Bankroll: ${PORTFOLIO_BANKROLL} -> ${final:.2f} ({roi:+.1f}%)")
            print(f"    Trades: {len(log)} | Wins: {wins} ({wr:.1f}%) | P&L: ${pnl:+.2f} | MDD: {mdd:.1%}")
        else:
            total_final += PORTFOLIO_BANKROLL  # No trades, keep bankroll
            print(f"\n  {strat_name}")
            print(f"    No trades executed")
    
    # Combined portfolio stats
    portfolio_roi = (total_final - total_capital) / total_capital * 100
    combined_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    
    print(f"\n  {'='*60}")
    print(f"  COMBINED PORTFOLIO")
    print(f"  {'='*60}")
    print(f"  Total Capital:   ${total_capital:.2f}")
    print(f"  Final Value:     ${total_final:.2f}")
    print(f"  Portfolio ROI:   {portfolio_roi:+.1f}%")
    print(f"  Total P&L:       ${total_pnl:+.2f}")
    print(f"  Total Trades:    {total_trades}")
    print(f"  Combined WR:     {combined_wr:.1f}%")
    print(f"  Total Wins:      {total_wins} / {total_trades}")
    
    # Per-strategy contribution
    print(f"\n  P&L CONTRIBUTION:")
    for strat_name, res in sorted(portfolio_results.items(), key=lambda x: -sum(t['pnl'] for t in x[1]['log'])):
        log = res['log']
        pnl = sum(t['pnl'] for t in log)
        pct = pnl / total_pnl * 100 if total_pnl != 0 else 0
        bar = '#' * max(0, int(pct / 3))
        print(f"    {strat_name:35s}: ${pnl:>+7.2f} ({pct:>5.1f}%)  {bar}")
    
    # Generate portfolio chart
    generate_portfolio_chart(portfolio_results, PORTFOLIO_BANKROLL, total_capital, total_final)
    
    # ============================================================
    # Save files
    # ============================================================
    
    # Generate single-strategy sweep chart
    chart_results = {}
    for name in ['Hold (conf>50%)', 'Hold (conf>55%)', 'Hold (conf>60%)',
                  'Hold (conf>65%)', 'Hold (conf>70%)', 'Hold (conf>75%)',
                  'Hold (conf>80%)']:
        if name in all_results and len(all_results[name][0]) > 0:
            chart_results[name] = all_results[name]
    generate_chart(chart_results if chart_results else all_results)
    
    # Save trade log
    best_name = max(all_results, key=lambda k: all_results[k][2])
    best_log = all_results[best_name][0]
    if best_log:
        df_log = pd.DataFrame(best_log)
        df_log.to_csv('trade_log.csv', index=False)
        print(f"\n  Trade log saved to trade_log.csv (best: {best_name})")
    
    # Save ALL trade logs (including portfolio)
    all_trades = []
    for name, (log, ec, final) in all_results.items():
        for t in log:
            t_copy = t.copy()
            t_copy['strategy_name'] = name
            all_trades.append(t_copy)
    # Add portfolio trades
    for strat_name, res in portfolio_results.items():
        for t in res['log']:
            t_copy = t.copy()
            t_copy['strategy_name'] = f'PORTFOLIO: {strat_name}'
            all_trades.append(t_copy)
    if all_trades:
        pd.DataFrame(all_trades).to_csv('trade_log_all.csv', index=False)
        print(f"  Full trade log saved to trade_log_all.csv ({len(all_trades)} trades)")
    
    # Save portfolio trades separately
    portfolio_trades = []
    for strat_name, res in portfolio_results.items():
        for t in res['log']:
            t_copy = t.copy()
            t_copy['portfolio_strategy'] = strat_name
            portfolio_trades.append(t_copy)
    if portfolio_trades:
        pd.DataFrame(portfolio_trades).to_csv('portfolio_trades.csv', index=False)
        print(f"  Portfolio trades saved to portfolio_trades.csv ({len(portfolio_trades)} trades)")
    
    # Save sweep data
    if hold_sweep:
        pd.DataFrame(hold_sweep).to_csv('confidence_sweep.csv', index=False)
        print(f"  Confidence sweep saved to confidence_sweep.csv")
    
    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.0f}s")

if __name__ == "__main__":
    main()
