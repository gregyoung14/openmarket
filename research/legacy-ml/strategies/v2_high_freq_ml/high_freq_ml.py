"""
ULTIMATE ML BENCH v2 - Polymarket BTC Prediction
=================================================
Fixes from v1 analysis:
1. MASSIVE DATA LOSS: 37985 -> 780 rows at 1s (97.9% loss!) due to NaN from
   rolling windows + Polymarket merge gaps. FIX: Use smaller windows, forward-fill
   Polymarket data, handle NaN properly.
2. CLASS IMBALANCE: 61.6% DOWN at 1s. FIX: use scale_pos_weight.
3. 5s MODEL NOT LEARNING (iteration 0): Too much regularization + too few useful
   features after NaN drop. FIX: reduce gamma, fill NaN instead of drop.
4. POLYMARKET MERGE: Inner join kills rows where Polymarket has no tick in that
   exact second. FIX: Left join + forward fill.
5. FEATURE WINDOWS: Using window=20 on 1s data requires 20s of warmup but the
   Polymarket data only overlaps for part of the range. FIX: adaptive windows.
"""

import sqlite3
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.metrics import accuracy_score, classification_report, log_loss
import warnings
warnings.filterwarnings('ignore')
import os
import time

DB_PATH = 'polymarket_btc_data.db'

# ========================================================================
# DATA LOADING
# ========================================================================

def load_binance_trades(conn):
    print("Loading binance_trades...")
    df = pd.read_sql_query(
        "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC",
        conn
    )
    return df

def load_polymarket_ticks(conn):
    print("Loading polymarket_ticks_ms...")
    df = pd.read_sql_query(
        """SELECT source_ts_ms, side_label, price, best_bid, best_ask, size 
           FROM polymarket_ticks_ms 
           WHERE event_type = 'price_change'
           ORDER BY source_ts_ms ASC""",
        conn
    )
    return df

def load_lag_pairs(conn):
    print("Loading lag_pairs_ms...")
    df = pd.read_sql_query(
        """SELECT paired_at_ms, side_label, lead_lag_ms, quality_flag
           FROM lag_pairs_ms
           ORDER BY paired_at_ms ASC""",
        conn
    )
    return df

# ========================================================================
# FEATURE ENGINEERING
# ========================================================================

def aggregate_binance(df_trades, ms):
    """Aggregate trades into buckets."""
    df = df_trades.copy()
    df['bucket'] = (df['trade_time'] // ms) * ms
    
    # Pre-compute buy/sell masks
    is_buy = df['is_buyer_maker'] == 0
    is_sell = df['is_buyer_maker'] == 1
    
    # Basic OHLCV
    g = df.groupby('bucket')
    agg = g['price'].agg(open_price='first', high_price='max', low_price='min', close_price='last', price_std='std').reset_index()
    agg['volume'] = g['quantity'].sum().values
    agg['trade_count'] = g['price'].count().values
    agg['avg_trade_size'] = g['quantity'].mean().values
    agg['max_trade_size'] = g['quantity'].max().values
    
    # Buy/sell volume
    buy_vol = df[is_buy].groupby('bucket')['quantity'].sum().rename('buy_volume')
    sell_vol = df[is_sell].groupby('bucket')['quantity'].sum().rename('sell_volume')
    agg = agg.merge(buy_vol, on='bucket', how='left')
    agg = agg.merge(sell_vol, on='bucket', how='left')
    agg['buy_volume'] = agg['buy_volume'].fillna(0)
    agg['sell_volume'] = agg['sell_volume'].fillna(0)
    
    # VWAP
    df['pv'] = df['price'] * df['quantity']
    vwap = (df.groupby('bucket')['pv'].sum() / df.groupby('bucket')['quantity'].sum()).rename('vwap')
    agg = agg.merge(vwap, on='bucket', how='left')
    
    agg['ts'] = pd.to_datetime(agg['bucket'], unit='ms')
    agg.set_index('ts', inplace=True)
    agg.drop(columns=['bucket'], inplace=True)
    agg.sort_index(inplace=True)
    return agg

def aggregate_polymarket(df_poly, ms):
    """Aggregate Polymarket ticks per bucket, separately for UP/DOWN."""
    df = df_poly.copy()
    df['bucket'] = (df['source_ts_ms'] // ms) * ms
    
    result = pd.DataFrame()
    
    for side in ['UP', 'DOWN']:
        s = df[df['side_label'] == side]
        if len(s) == 0:
            continue
        g = s.groupby('bucket')
        side_l = side.lower()
        
        agg = pd.DataFrame()
        agg[f'poly_{side_l}_last'] = g['price'].last()
        agg[f'poly_{side_l}_mean'] = g['price'].mean()
        agg[f'poly_{side_l}_bid'] = g['best_bid'].last()
        agg[f'poly_{side_l}_ask'] = g['best_ask'].last()
        agg[f'poly_{side_l}_vol'] = g['size'].sum()
        agg[f'poly_{side_l}_ticks'] = g['price'].count()
        
        if result.empty:
            result = agg
        else:
            result = result.join(agg, how='outer')
    
    if result.empty:
        return pd.DataFrame()
    
    result.index = pd.to_datetime(result.index, unit='ms')
    result.sort_index(inplace=True)
    return result

def aggregate_lag_pairs(df_lag, ms):
    """Aggregate lag pair data."""
    df = df_lag[df_lag['quality_flag'].isin(['tight', 'medium'])].copy()
    df['bucket'] = (df['paired_at_ms'] // ms) * ms
    
    g = df.groupby('bucket')
    agg = pd.DataFrame()
    agg['lag_mean'] = g['lead_lag_ms'].mean()
    agg['lag_std'] = g['lead_lag_ms'].std()
    agg['lag_pos_ratio'] = g['lead_lag_ms'].apply(lambda x: (x > 0).mean())
    agg['lag_count'] = g['lead_lag_ms'].count()
    
    agg.index = pd.to_datetime(agg.index, unit='ms')
    agg.sort_index(inplace=True)
    return agg

def build_features(df):
    """Build derived features with MINIMAL data loss."""
    df = df.copy()
    n = len(df)
    
    # ---- CORE PRICE ----
    df['returns'] = df['close_price'].pct_change()
    df['hl_range'] = (df['high_price'] - df['low_price']) / (df['close_price'] + 1e-9)
    df['co_range'] = (df['close_price'] - df['open_price']) / (df['open_price'] + 1e-9)
    
    # VWAP deviation
    if 'vwap' in df.columns:
        df['vwap_dev'] = (df['close_price'] - df['vwap']) / (df['vwap'] + 1e-9)
    
    # Intra-bar volatility
    if 'price_std' in df.columns:
        df['intrabar_vol'] = df['price_std'] / (df['close_price'] + 1e-9)
    
    # ---- ORDER FLOW (THE BIGGEST EDGE) ----
    total = df['buy_volume'] + df['sell_volume'] + 1e-9
    df['ofi'] = (df['buy_volume'] - df['sell_volume']) / total
    df['buy_ratio'] = df['buy_volume'] / total
    
    # Rolling OFI with SMALL windows to preserve rows
    for w in [3, 5, 10]:
        df[f'ofi_ma_{w}'] = df['ofi'].rolling(w, min_periods=1).mean()
        df[f'ofi_accel_{w}'] = df['ofi'] - df[f'ofi_ma_{w}']
    
    # Cumulative OFI (resets every 100 bars)
    df['cum_ofi'] = df['ofi'].rolling(50, min_periods=1).sum()
    
    # ---- TRADE DYNAMICS ----
    df['tc_change'] = df['trade_count'].pct_change()
    df['tc_ma5'] = df['trade_count'].rolling(5, min_periods=1).mean()
    df['rel_tc'] = df['trade_count'] / (df['tc_ma5'] + 1e-9)
    
    if 'avg_trade_size' in df.columns:
        df['ats_ma10'] = df['avg_trade_size'].rolling(10, min_periods=1).mean()
        df['rel_ats'] = df['avg_trade_size'] / (df['ats_ma10'] + 1e-9)
    if 'max_trade_size' in df.columns:
        df['mts_ma10'] = df['max_trade_size'].rolling(10, min_periods=1).mean()
        df['whale_indicator'] = df['max_trade_size'] / (df['mts_ma10'] + 1e-9)
    
    # ---- VOLATILITY ----
    df['vol_3'] = df['returns'].rolling(3, min_periods=1).std()
    df['vol_10'] = df['returns'].rolling(10, min_periods=1).std()
    df['vol_ratio'] = df['vol_3'] / (df['vol_10'] + 1e-9)
    
    # ---- MOMENTUM ----
    for p in [3, 5, 10]:
        df[f'roc_{p}'] = df['close_price'].pct_change(periods=p)
    
    # RSI (window=10 to save rows)
    delta = df['close_price'].diff()
    gain = delta.where(delta > 0, 0).rolling(10, min_periods=1).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(10, min_periods=1).mean()
    df['rsi'] = 100 - (100 / (1 + gain / (loss + 1e-9)))
    
    # EMA cross
    df['ema_f'] = df['close_price'].ewm(span=5, adjust=False).mean()
    df['ema_s'] = df['close_price'].ewm(span=15, adjust=False).mean()
    df['ema_cross'] = (df['ema_f'] - df['ema_s']) / (df['ema_s'] + 1e-9)
    
    # ---- POLYMARKET FEATURES ----
    if 'poly_up_last' in df.columns:
        df['poly_up_prob'] = df['poly_up_last']
        df['poly_spread_up'] = df.get('poly_up_ask', pd.Series(dtype=float)) - df.get('poly_up_bid', pd.Series(dtype=float))
        df['poly_spread_down'] = df.get('poly_down_ask', pd.Series(dtype=float)) - df.get('poly_down_bid', pd.Series(dtype=float))
        
        # Polymarket momentum
        df['poly_up_mom3'] = df['poly_up_last'].pct_change(periods=3)
        df['poly_up_mom5'] = df['poly_up_last'].pct_change(periods=5)
        df['poly_up_diff'] = df['poly_up_last'].diff()
        
        # Polymarket volume signal
        if 'poly_up_vol' in df.columns and 'poly_down_vol' in df.columns:
            ptv = df['poly_up_vol'] + df['poly_down_vol'] + 1e-9
            df['poly_vol_ratio'] = df['poly_up_vol'] / ptv
        
        # THE KEY: Polymarket-Binance divergence
        df['poly_binance_div'] = df['poly_up_last'].pct_change() - df['returns']
    
    # ---- LAG PAIR FEATURES ----
    if 'lag_mean' in df.columns:
        df['lag_dir'] = np.sign(df['lag_mean'])
        df['lag_speed_chg'] = df['lag_mean'].diff()
    
    # ---- LAGS (most critical for temporal patterns) ----
    for i in range(1, 6):
        df[f'ret_lag{i}'] = df['returns'].shift(i)
        df[f'ofi_lag{i}'] = df['ofi'].shift(i)
    
    if 'poly_up_prob' in df.columns:
        for i in range(1, 4):
            df[f'poly_lag{i}'] = df['poly_up_prob'].shift(i)
    
    # ---- TARGET ----
    df['target'] = (df['close_price'].shift(-1) > df['close_price']).astype(int)
    
    # Drop ONLY the last row (no target) and first few (no lags)
    # Fill remaining NaN with 0 instead of dropping rows
    df = df.iloc[5:-1]  # Trim warmup and last row
    
    # For Polymarket/lag columns that are NaN (no data in that bucket), fill
    poly_cols = [c for c in df.columns if 'poly' in c or 'lag_' in c]
    df[poly_cols] = df[poly_cols].ffill().fillna(0)
    
    # Any remaining NaN
    df = df.fillna(0)
    
    # Replace inf
    df = df.replace([np.inf, -np.inf], 0)
    
    return df

def get_features(df):
    """Select only derived feature columns."""
    raw = {
        'target', 'open_price', 'high_price', 'low_price', 'close_price',
        'volume', 'buy_volume', 'sell_volume', 'vwap', 'price_std',
        'avg_trade_size', 'max_trade_size', 'trade_count', 'returns',
        'ema_f', 'ema_s', 'tc_ma5', 'ats_ma10', 'mts_ma10',
        'poly_up_last', 'poly_up_mean', 'poly_up_bid', 'poly_up_ask',
        'poly_down_last', 'poly_down_mean', 'poly_down_bid', 'poly_down_ask',
        'poly_up_vol', 'poly_down_vol', 'poly_up_ticks', 'poly_down_ticks',
        'lag_count', 'lag_std',
    }
    return [c for c in df.columns if c not in raw]

def run_xgboost(df, label):
    feats = get_features(df)
    X = df[feats]
    y = df['target']
    
    print(f"\n{'='*70}")
    print(f" {label}")
    print(f" Rows: {len(df)} | Features: {len(feats)}")
    up_pct = y.mean()
    print(f" Target: UP={up_pct:.1%}, DOWN={1-up_pct:.1%}")
    print(f"{'='*70}")
    
    if len(df) < 100:
        print(f"SKIP: {len(df)} rows")
        return {}
    
    # Smart split
    n = len(df)
    train_end = int(n * 0.7)
    val_end = int(n * 0.85)
    
    X_train, y_train = X.iloc[:train_end], y.iloc[:train_end]
    X_val, y_val = X.iloc[train_end:val_end], y.iloc[train_end:val_end]
    X_test, y_test = X.iloc[val_end:], y.iloc[val_end:]
    
    # Class weight
    neg = (y_train == 0).sum()
    pos = (y_train == 1).sum()
    spw = neg / (pos + 1e-9)
    
    model = xgb.XGBClassifier(
        n_estimators=2000,
        max_depth=4,
        learning_rate=0.01,
        subsample=0.75,
        colsample_bytree=0.6,
        colsample_bylevel=0.6,
        gamma=1,
        min_child_weight=3,
        reg_alpha=0.05,
        reg_lambda=1.0,
        scale_pos_weight=spw,
        eval_metric='logloss',
        random_state=42,
        early_stopping_rounds=30,
    )
    
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    
    acc = accuracy_score(y_test, y_pred)
    ll = log_loss(y_test, y_prob)
    
    print(f"\n  Accuracy:     {acc:.4f}")
    print(f"  Log Loss:     {ll:.4f}")
    print(f"  Best Iter:    {model.best_iteration}")
    print(classification_report(y_test, y_pred, target_names=['DOWN', 'UP'], digits=4))
    
    # Top features
    imps = pd.Series(model.feature_importances_, index=feats).sort_values(ascending=False)
    print("  Top 10 Features:")
    for f, v in imps.head(10).items():
        print(f"    {f:30s} {v:.4f}")
    
    # Confidence bands
    print("\n  Confidence Bands:")
    for t in [0.50, 0.52, 0.55, 0.58, 0.60, 0.65]:
        mask = (y_prob > t) | (y_prob < (1-t))
        if mask.sum() > 5:
            ca = accuracy_score(y_test[mask], y_pred[mask])
            print(f"    >{t:.2f}: Acc={ca:.4f} ({mask.sum()}/{len(y_test)} = {mask.mean():.1%})")
    
    return {'acc': acc, 'log_loss': ll, 'model': model, 'features': feats}

# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    
    df_trades = load_binance_trades(conn)
    df_poly = load_polymarket_ticks(conn)
    df_lag = load_lag_pairs(conn)
    conn.close()
    
    print(f"\nData loaded: {len(df_trades)} trades, {len(df_poly)} poly ticks, {len(df_lag)} lag pairs\n")
    
    timeframes = {'1s': 1000, '5s': 5000, '15s': 15000, '30s': 30000}
    results = {}
    
    for label, ms in timeframes.items():
        print(f"\n{'#'*70}")
        print(f"# {label}")
        print(f"{'#'*70}")
        
        try:
            # Aggregate
            df_b = aggregate_binance(df_trades, ms)
            df_p = aggregate_polymarket(df_poly, ms)
            df_l = aggregate_lag_pairs(df_lag, ms)
            
            # Merge (LEFT join — keep all Binance rows)
            df = df_b.copy()
            if not df_p.empty:
                df = df.join(df_p, how='left')
            if not df_l.empty:
                df = df.join(df_l, how='left')
            
            print(f"After merge: {len(df)} rows, {len(df.columns)} cols")
            
            # Features
            df = build_features(df)
            print(f"After features: {len(df)} rows")
            
            # Run
            r = run_xgboost(df, label)
            if r:
                results[label] = r['acc']
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
    
    # Summary
    elapsed = time.time() - t0
    print(f"\n\n{'='*70}")
    print(f" RESULTS  ({elapsed:.0f}s)")
    print(f"{'='*70}")
    for l in sorted(results, key=results.get, reverse=True):
        a = results[l]
        bar = '█' * int(a * 60)
        s = '🚀' if a > 0.60 else '🔥' if a > 0.55 else '⚡' if a > 0.53 else ''
        print(f"  {l:>4s}: {a:.4f} {bar} {s}")
    
    if results:
        print(f"\n  Best: {max(results, key=results.get)} at {max(results.values()):.4f}")

if __name__ == "__main__":
    main()
