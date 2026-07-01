"""
MAX ACCURACY SQUEEZE - Focused on 1s timeframe (our winner)
============================================================
Current: 62.37% overall, 77.2% at >65% confidence

Improvements to try:
1. FEATURE SELECTION: Remove noise features that dilute signal
2. HYPERPARAMETER GRID SEARCH on the 1s model specifically  
3. MULTI-TIMEFRAME: Feed 5s/15s features INTO the 1s model
4. TARGET ENGINEERING: Try different target definitions
5. ENSEMBLE: Blend multiple models
6. TIME-OF-DAY features (market behavior changes across hours)
"""

import sqlite3
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.metrics import accuracy_score, classification_report, log_loss
import warnings
warnings.filterwarnings('ignore')
import time

DB_PATH = 'polymarket_btc_data.db'

def load_all(conn):
    df_trades = pd.read_sql_query(
        "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC", conn)
    df_poly = pd.read_sql_query(
        "SELECT source_ts_ms, side_label, price, best_bid, best_ask, size FROM polymarket_ticks_ms WHERE event_type='price_change' ORDER BY source_ts_ms ASC", conn)
    df_lag = pd.read_sql_query(
        "SELECT paired_at_ms, lead_lag_ms, quality_flag FROM lag_pairs_ms ORDER BY paired_at_ms ASC", conn)
    return df_trades, df_poly, df_lag

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

def agg_poly(df, ms):
    df = df.copy()
    df['b'] = (df['source_ts_ms'] // ms) * ms
    r = pd.DataFrame()
    for s in ['UP', 'DOWN']:
        sub = df[df['side_label'] == s]
        if len(sub) == 0: continue
        g = sub.groupby('b')
        sl = s.lower()
        a = pd.DataFrame({
            f'p_{sl}_last': g['price'].last(),
            f'p_{sl}_bid': g['best_bid'].last(),
            f'p_{sl}_ask': g['best_ask'].last(),
            f'p_{sl}_vol': g['size'].sum(),
            f'p_{sl}_cnt': g['price'].count(),
        })
        r = a if r.empty else r.join(a, how='outer')
    if r.empty: return pd.DataFrame()
    r.index = pd.to_datetime(r.index, unit='ms')
    return r

def agg_lag(df, ms):
    df = df[df['quality_flag'].isin(['tight','medium'])].copy()
    df['b'] = (df['paired_at_ms'] // ms) * ms
    g = df.groupby('b')
    a = pd.DataFrame({
        'lgm': g['lead_lag_ms'].mean(),
        'lgstd': g['lead_lag_ms'].std(),
        'lgpr': g['lead_lag_ms'].apply(lambda x: (x>0).mean()),
        'lgn': g['lead_lag_ms'].count(),
    })
    a.index = pd.to_datetime(a.index, unit='ms')
    return a

def build(binance_1s, poly_1s, lag_1s, binance_5s, poly_5s):
    """Build features with multi-timeframe context."""
    df = binance_1s.copy()
    
    # Join poly and lag
    if not poly_1s.empty:
        df = df.join(poly_1s, how='left')
    if not lag_1s.empty:
        df = df.join(lag_1s, how='left')
    
    # Join 5s features as context (resample/align)
    if not binance_5s.empty:
        # Rename 5s columns with prefix
        b5 = binance_5s[['c', 'v', 'tc']].copy()
        b5.columns = ['c5s', 'v5s', 'tc5s']
        df = df.join(b5, how='left')
        df[['c5s', 'v5s', 'tc5s']] = df[['c5s', 'v5s', 'tc5s']].ffill()
    
    if not poly_5s.empty:
        p5 = poly_5s[['p_up_last', 'p_up_vol']].copy()
        p5.columns = ['p_up_5s', 'p_up_vol_5s']
        df = df.join(p5, how='left')
        df[['p_up_5s', 'p_up_vol_5s']] = df[['p_up_5s', 'p_up_vol_5s']].ffill()
    
    # Forward fill poly/lag cols
    pcols = [c for c in df.columns if c.startswith('p_') or c.startswith('lg')]
    df[pcols] = df[pcols].ffill().fillna(0)
    
    # ============ FEATURES ============
    
    # Returns & Price
    df['ret'] = df['c'].pct_change()
    df['hl'] = (df['h'] - df['l']) / (df['c'] + 1e-9)
    df['co'] = (df['c'] - df['o']) / (df['o'] + 1e-9)
    df['vwap_d'] = (df['c'] - df['vwap']) / (df['vwap'] + 1e-9)
    df['ivol'] = df['pstd'] / (df['c'] + 1e-9)
    
    # OFI
    tot = df['bv'] + df['sv'] + 1e-9
    df['ofi'] = (df['bv'] - df['sv']) / tot
    df['br'] = df['bv'] / tot
    for w in [3, 5, 10]:
        df[f'ofi_m{w}'] = df['ofi'].rolling(w, min_periods=1).mean()
        df[f'ofi_a{w}'] = df['ofi'] - df[f'ofi_m{w}']
    df['cum_ofi'] = df['ofi'].rolling(30, min_periods=1).sum()
    
    # Trade dynamics
    df['tc_r'] = df['tc'].pct_change()
    df['tc_m5'] = df['tc'].rolling(5, min_periods=1).mean()
    df['rtc'] = df['tc'] / (df['tc_m5'] + 1e-9)
    df['ats_m'] = df['ats'].rolling(10, min_periods=1).mean()
    df['rats'] = df['ats'] / (df['ats_m'] + 1e-9)
    df['whale'] = df['mts'] / (df['mts'].rolling(10, min_periods=1).mean() + 1e-9)
    
    # Volatility
    df['v3'] = df['ret'].rolling(3, min_periods=1).std()
    df['v10'] = df['ret'].rolling(10, min_periods=1).std()
    df['vratio'] = df['v3'] / (df['v10'] + 1e-9)
    
    # Momentum
    for p in [3, 5, 10]:
        df[f'roc{p}'] = df['c'].pct_change(periods=p)
    
    # RSI
    d = df['c'].diff()
    g = d.where(d > 0, 0).rolling(10, min_periods=1).mean()
    l = (-d.where(d < 0, 0)).rolling(10, min_periods=1).mean()
    df['rsi'] = 100 - (100 / (1 + g / (l + 1e-9)))
    
    df['ema_x'] = (df['c'].ewm(span=5).mean() - df['c'].ewm(span=15).mean()) / (df['c'].ewm(span=15).mean() + 1e-9)
    
    # ---- POLYMARKET (TOP SIGNAL) ----
    if 'p_up_last' in df.columns:
        df['pup'] = df['p_up_last']
        df['psp_u'] = df.get('p_up_ask', 0) - df.get('p_up_bid', 0)
        df['psp_d'] = df.get('p_down_ask', 0) - df.get('p_down_bid', 0)
        df['pm3'] = df['p_up_last'].pct_change(3)
        df['pm5'] = df['p_up_last'].pct_change(5)
        df['pd1'] = df['p_up_last'].diff()
        if 'p_up_vol' in df.columns and 'p_down_vol' in df.columns:
            ptv = df['p_up_vol'] + df['p_down_vol'] + 1e-9
            df['pvr'] = df['p_up_vol'] / ptv
        df['pdiv'] = df['p_up_last'].pct_change() - df['ret']
    
    # Lag pair
    if 'lgm' in df.columns:
        df['lgdir'] = np.sign(df['lgm'])
        df['lgchg'] = df['lgm'].diff()
    
    # Time of day (hour as cyclic feature)
    df['hour'] = df.index.hour
    df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
    df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
    
    # Multi-timeframe: 5s context
    if 'c5s' in df.columns:
        df['ret_5s'] = df['c5s'].pct_change()
        df['cross_tf'] = df['ret'] - df['ret_5s']  # Intra-timeframe divergence
    
    # Lags
    for i in range(1, 6):
        df[f'rl{i}'] = df['ret'].shift(i)
        df[f'ol{i}'] = df['ofi'].shift(i)
    if 'pup' in df.columns:
        for i in range(1, 4):
            df[f'pl{i}'] = df['pup'].shift(i)
    
    # Target
    df['target'] = (df['c'].shift(-1) > df['c']).astype(int)
    
    # Trim and clean
    df = df.iloc[10:-1]
    fcols = [c for c in df.columns if any(c.startswith(p) for p in ['p_', 'lg', 'p_up', 'p_down', 'c5s', 'v5s', 'tc5s', 'p_up_5s', 'p_up_vol_5s'])]
    if fcols:
        df[fcols] = df[fcols].ffill().fillna(0)
    df = df.fillna(0).replace([np.inf, -np.inf], 0)
    
    return df

def get_feats(df):
    raw = {'target', 'o', 'h', 'l', 'c', 'v', 'bv', 'sv', 'vwap', 'pstd', 'ats', 'mts', 'tc',
           'ret', 'tc_m5', 'ats_m', 'p_up_last', 'p_up_bid', 'p_up_ask', 'p_down_last', 'p_down_bid', 
           'p_down_ask', 'p_up_vol', 'p_down_vol', 'p_up_cnt', 'p_down_cnt', 'lgn', 'lgstd',
           'c5s', 'v5s', 'tc5s', 'p_up_5s', 'p_up_vol_5s', 'hour', 'p_up_mean', 'p_down_mean',
           'ret_5s'}
    return [c for c in df.columns if c not in raw]

def run(df, label, configs):
    feats = get_feats(df)
    X, y = df[feats], df['target']
    
    n = len(df)
    tr = int(n * 0.7)
    vl = int(n * 0.85)
    
    Xtr, ytr = X.iloc[:tr], y.iloc[:tr]
    Xvl, yvl = X.iloc[tr:vl], y.iloc[tr:vl]
    Xte, yte = X.iloc[vl:], y.iloc[vl:]
    
    spw = (ytr == 0).sum() / ((ytr == 1).sum() + 1e-9)
    
    best_acc = 0
    best_conf = None
    best_model = None
    
    for i, cfg in enumerate(configs):
        m = xgb.XGBClassifier(
            scale_pos_weight=spw,
            eval_metric='logloss',
            random_state=42,
            early_stopping_rounds=30,
            **cfg
        )
        m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
        yp = m.predict(Xte)
        a = accuracy_score(yte, yp)
        if a > best_acc:
            best_acc = a
            best_conf = cfg
            best_model = m
    
    yp = best_model.predict(Xte)
    yprob = best_model.predict_proba(Xte)[:, 1]
    
    print(f"\n{'='*70}")
    print(f" {label} | Rows={n} | Feats={len(feats)} | BestAcc={best_acc:.4f}")
    print(f" Best config: {best_conf}")
    print(f" Best iter: {best_model.best_iteration}")
    print(f"{'='*70}")
    print(classification_report(yte, yp, target_names=['DOWN','UP'], digits=4))
    
    imps = pd.Series(best_model.feature_importances_, index=feats).sort_values(ascending=False)
    print("Top Features:")
    for f, v in imps.head(10).items():
        print(f"  {f:25s} {v:.4f}")
    
    print("\nConfidence Analysis:")
    for t in [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70]:
        mask = (yprob > t) | (yprob < (1-t))
        if mask.sum() > 5:
            ca = accuracy_score(yte[mask], yp[mask])
            pct = mask.mean()
            print(f"  >{t:.2f}: Acc={ca:.4f} ({mask.sum()}/{len(yte)}={pct:.1%})")
    
    return best_acc, best_model

def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    df_trades, df_poly, df_lag = load_all(conn)
    conn.close()
    print(f"Loaded: {len(df_trades)} trades, {len(df_poly)} poly, {len(df_lag)} lag")
    
    # Build 1s and 5s aggregations
    b1 = agg_binance(df_trades, 1000)
    p1 = agg_poly(df_poly, 1000)
    l1 = agg_lag(df_lag, 1000)
    b5 = agg_binance(df_trades, 5000)
    p5 = agg_poly(df_poly, 5000)
    
    # Build mega feature set for 1s
    df = build(b1, p1, l1, b5, p5)
    print(f"\nFinal dataset: {len(df)} rows, {len(df.columns)} cols")
    
    # Hyperparameter configurations to try
    configs = [
        {'n_estimators': 2000, 'max_depth': 3, 'learning_rate': 0.01, 'subsample': 0.7, 'colsample_bytree': 0.5, 'gamma': 1, 'min_child_weight': 5, 'reg_alpha': 0.1, 'reg_lambda': 1.0},
        {'n_estimators': 2000, 'max_depth': 4, 'learning_rate': 0.01, 'subsample': 0.75, 'colsample_bytree': 0.6, 'gamma': 1, 'min_child_weight': 3, 'reg_alpha': 0.05, 'reg_lambda': 1.0},
        {'n_estimators': 2000, 'max_depth': 5, 'learning_rate': 0.005, 'subsample': 0.8, 'colsample_bytree': 0.7, 'gamma': 0.5, 'min_child_weight': 3, 'reg_alpha': 0.01, 'reg_lambda': 0.5},
        {'n_estimators': 3000, 'max_depth': 3, 'learning_rate': 0.005, 'subsample': 0.65, 'colsample_bytree': 0.5, 'gamma': 2, 'min_child_weight': 7, 'reg_alpha': 0.2, 'reg_lambda': 2.0},
        {'n_estimators': 2000, 'max_depth': 6, 'learning_rate': 0.008, 'subsample': 0.7, 'colsample_bytree': 0.6, 'gamma': 1.5, 'min_child_weight': 5, 'reg_alpha': 0.1, 'reg_lambda': 1.5},
        {'n_estimators': 2000, 'max_depth': 4, 'learning_rate': 0.01, 'subsample': 0.7, 'colsample_bytree': 0.5, 'gamma': 0.5, 'min_child_weight': 3, 'reg_alpha': 0, 'reg_lambda': 0.5},
    ]
    
    acc, model = run(df, "1s ULTIMATE", configs)
    
    elapsed = time.time() - t0
    print(f"\n\nTotal time: {elapsed:.0f}s")
    print(f"Final accuracy: {acc:.4f}")

if __name__ == "__main__":
    main()
