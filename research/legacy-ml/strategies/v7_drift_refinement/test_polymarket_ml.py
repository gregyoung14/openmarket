"""
POLYMARKET-ALIGNED ML TEST
===========================
Predicts the ACTUAL Polymarket resolution: will BTC at the END of a 15-min
window be HIGHER or LOWER than at the OPEN?

Key differences from high_freq_ml.py:
  - Target = 15-min market resolution (not next-tick direction)
  - Features anchored to the 15-min cycle open price
  - Drift/momentum estimation from early seconds to project final outcome
  - EMA regime features (user reported these look promising on charts)
  - Cross-market memory (streak detection, mean reversion)
  - Entry-time simulation (predict at 30s, 60s, 120s, 300s into market)
"""

import sqlite3
import pandas as pd
import numpy as np
import xgboost as xgb
import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, log_loss
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')
import time

DB_PATH = 'polymarket_btc_data.db'

# 15-minute market boundaries (epoch seconds)
MARKET_DURATION = 900  # seconds

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

# ========================================================================
# BUILD 15-MINUTE MARKETS FROM BINANCE DATA
# ========================================================================

def build_15min_markets(df_trades):
    """
    Reconstruct 15-minute market windows from Binance trade data.
    Markets open at :00, :15, :30, :45 of each hour.
    """
    print("\nBuilding 15-minute market windows...")
    
    # Find the time range
    min_ms = df_trades['trade_time'].min()
    max_ms = df_trades['trade_time'].max()
    
    # Align to 15-minute boundaries (in seconds)
    min_s = int(min_ms / 1000)
    max_s = int(max_ms / 1000)
    
    # Round up to next 15-min boundary
    first_market_s = min_s - (min_s % 900) + 900
    
    markets = []
    market_start = first_market_s
    
    while market_start + 900 <= max_s:
        open_ms = market_start * 1000
        close_ms = (market_start + 900) * 1000
        
        # Get all trades in this 15-min window
        mask = (df_trades['trade_time'] >= open_ms) & (df_trades['trade_time'] < close_ms)
        window = df_trades[mask]
        
        if len(window) >= 50:  # Need minimum trades for a valid market
            open_price = window.iloc[0]['price']
            close_price = window.iloc[-1]['price']
            resolution = 'UP' if close_price > open_price else 'DOWN'
            
            markets.append({
                'market_open_ms': open_ms,
                'market_close_ms': close_ms,
                'open_price': open_price,
                'close_price': close_price,
                'resolution': resolution,
                'resolution_int': 1 if resolution == 'UP' else 0,
                'total_trades': len(window),
                'price_change_pct': (close_price - open_price) / open_price * 100,
            })
        
        market_start += 900
    
    df_markets = pd.DataFrame(markets)
    up_pct = df_markets['resolution_int'].mean()
    print(f"  Built {len(df_markets)} markets. UP={up_pct:.1%}, DOWN={1-up_pct:.1%}")
    print(f"  Avg trades per market: {df_markets['total_trades'].mean():.0f}")
    print(f"  Time range: {pd.to_datetime(df_markets['market_open_ms'].min(), unit='ms')} to {pd.to_datetime(df_markets['market_close_ms'].max(), unit='ms')}")
    
    return df_markets

# ========================================================================
# FEATURE ENGINEERING — ANCHORED TO 15-MIN CYCLE
# ========================================================================

def compute_market_features(df_trades, market, entry_seconds):
    """
    Compute features for a single market at a specific entry point.
    
    All features are anchored to the market open, not generic rolling windows.
    
    Args:
        df_trades: Full Binance trades DataFrame
        market: Dict with market_open_ms, open_price, etc.
        entry_seconds: How many seconds into the market we're computing features (e.g., 30, 60, 120)
    
    Returns:
        Dict of features, or None if insufficient data
    """
    open_ms = market['market_open_ms']
    entry_ms = open_ms + (entry_seconds * 1000)
    open_price = market['open_price']
    
    # All trades from market open to entry point
    mask = (df_trades['trade_time'] >= open_ms) & (df_trades['trade_time'] < entry_ms)
    window = df_trades[mask]
    
    if len(window) < 10:
        return None
    
    # Also get pre-market trades (last 5 minutes before market open) for context
    pre_mask = (df_trades['trade_time'] >= open_ms - 300_000) & (df_trades['trade_time'] < open_ms)
    pre_window = df_trades[pre_mask]
    
    features = {}
    
    # ---- 1. SCOREBOARD: Where is price relative to open? ----
    current_price = window.iloc[-1]['price']
    features['price_vs_open'] = (current_price - open_price) / (open_price + 1e-9)
    features['price_vs_open_bps'] = features['price_vs_open'] * 10000  # In basis points
    
    # Price at various checkpoints
    n = len(window)
    features['price_vs_open_25pct'] = (window.iloc[n//4]['price'] - open_price) / (open_price + 1e-9) if n > 4 else 0
    features['price_vs_open_50pct'] = (window.iloc[n//2]['price'] - open_price) / (open_price + 1e-9) if n > 2 else 0
    features['price_vs_open_75pct'] = (window.iloc[3*n//4]['price'] - open_price) / (open_price + 1e-9) if n > 4 else 0

    # ---- 2. DRIFT ESTIMATION (Brownian motion with drift) ----
    # Estimate drift μ and volatility σ from observed data
    # Then project: P(price_900 > price_0) = Φ(μ√T / σ)
    prices = window['price'].values
    log_returns = np.diff(np.log(prices + 1e-9))
    
    if len(log_returns) > 5:
        dt = entry_seconds / len(log_returns)  # Average time per observation
        mu = np.mean(log_returns) / (dt + 1e-9)       # Drift per second
        sigma = np.std(log_returns) / (np.sqrt(dt) + 1e-9)  # Vol per sqrt(second)
        
        remaining_seconds = 900 - entry_seconds
        
        features['drift_mu'] = mu
        features['drift_sigma'] = sigma
        features['drift_sharpe'] = mu / (sigma + 1e-9)
        
        # Projected probability that price ends higher (Brownian motion formula)
        if sigma > 0:
            z = mu * np.sqrt(remaining_seconds) / sigma
            features['drift_prob_up'] = norm.cdf(z)
        else:
            features['drift_prob_up'] = 0.5
        
        # Drift projected price change
        features['drift_projected_pct'] = mu * remaining_seconds * 100
    else:
        features['drift_mu'] = 0
        features['drift_sigma'] = 0
        features['drift_sharpe'] = 0
        features['drift_prob_up'] = 0.5
        features['drift_projected_pct'] = 0
    
    # ---- 3. EMA REGIME (user reported these look promising) ----
    # Compute EMAs on 1-second bars from market open to entry
    prices_series = pd.Series(prices)
    
    ema_fast = prices_series.ewm(span=10, adjust=False).mean().iloc[-1]
    ema_mid = prices_series.ewm(span=30, adjust=False).mean().iloc[-1]
    ema_slow = prices_series.ewm(span=60, adjust=False).mean().iloc[-1]
    
    features['ema_fast_vs_slow'] = (ema_fast - ema_slow) / (ema_slow + 1e-9)
    features['ema_fast_vs_mid'] = (ema_fast - ema_mid) / (ema_mid + 1e-9)
    features['ema_mid_vs_slow'] = (ema_mid - ema_slow) / (ema_slow + 1e-9)
    features['price_vs_ema_fast'] = (current_price - ema_fast) / (ema_fast + 1e-9)
    features['price_vs_ema_slow'] = (current_price - ema_slow) / (ema_slow + 1e-9)
    
    # EMA slope (acceleration) — are EMAs converging or diverging?
    if len(prices_series) > 15:
        ema_fast_prev = prices_series.ewm(span=10, adjust=False).mean().iloc[-10]
        features['ema_fast_slope'] = (ema_fast - ema_fast_prev) / (ema_fast_prev + 1e-9)
    else:
        features['ema_fast_slope'] = 0
    
    # ---- 4. ORDER FLOW IMBALANCE (cumulative from market open) ----
    is_buy = window['is_buyer_maker'] == 0
    buy_vol = window[is_buy]['quantity'].sum()
    sell_vol = window[~is_buy]['quantity'].sum()
    total_vol = buy_vol + sell_vol + 1e-9
    
    features['cum_ofi'] = (buy_vol - sell_vol) / total_vol
    features['buy_ratio'] = buy_vol / total_vol
    features['total_volume'] = total_vol
    
    # OFI in first half vs second half (is pressure building or fading?)
    half = len(window) // 2
    if half > 5:
        first_buy = window[:half][window[:half]['is_buyer_maker'] == 0]['quantity'].sum()
        first_sell = window[:half][window[:half]['is_buyer_maker'] != 0]['quantity'].sum()
        second_buy = window[half:][window[half:]['is_buyer_maker'] == 0]['quantity'].sum()
        second_sell = window[half:][window[half:]['is_buyer_maker'] != 0]['quantity'].sum()
        
        first_ofi = (first_buy - first_sell) / (first_buy + first_sell + 1e-9)
        second_ofi = (second_buy - second_sell) / (second_buy + second_sell + 1e-9)
        features['ofi_acceleration'] = second_ofi - first_ofi  # Positive = pressure building
    else:
        features['ofi_acceleration'] = 0
    
    # ---- 5. VOLATILITY REGIME ----
    features['realized_vol'] = np.std(log_returns) * np.sqrt(len(log_returns)) if len(log_returns) > 2 else 0
    features['high_low_range'] = (window['price'].max() - window['price'].min()) / (open_price + 1e-9)
    
    # Price path roughness (how "noisy" is the path?)
    if len(prices) > 5:
        total_path = np.sum(np.abs(np.diff(prices)))
        direct_path = abs(prices[-1] - prices[0])
        features['path_efficiency'] = direct_path / (total_path + 1e-9)  # 1 = straight line, 0 = noisy
    else:
        features['path_efficiency'] = 0
    
    # ---- 6. TRADE MICROSTRUCTURE ----
    features['avg_trade_size'] = window['quantity'].mean()
    features['trade_intensity'] = len(window) / entry_seconds  # Trades per second
    
    # Large trade detection (whale trades)
    trade_sizes = window['quantity'].values
    p90 = np.percentile(trade_sizes, 90) if len(trade_sizes) > 10 else 0
    features['large_trade_ratio'] = (window['quantity'] > p90).mean() if p90 > 0 else 0
    
    # VWAP
    vwap = (window['price'] * window['quantity']).sum() / (window['quantity'].sum() + 1e-9)
    features['price_vs_vwap'] = (current_price - vwap) / (vwap + 1e-9)

    # ---- 7. PRE-MARKET CONTEXT ----
    if len(pre_window) > 20:
        pre_open = pre_window.iloc[0]['price']
        pre_close = pre_window.iloc[-1]['price']
        features['pre_market_trend'] = (pre_close - pre_open) / (pre_open + 1e-9)
        
        pre_buy = pre_window[pre_window['is_buyer_maker'] == 0]['quantity'].sum()
        pre_sell = pre_window[pre_window['is_buyer_maker'] != 0]['quantity'].sum()
        features['pre_market_ofi'] = (pre_buy - pre_sell) / (pre_buy + pre_sell + 1e-9)
        
        pre_vol = pre_window['quantity'].sum()
        features['volume_surge'] = total_vol / (pre_vol / 300 * entry_seconds + 1e-9)  # Normalized
        
        pre_prices = pre_window['price'].values
        pre_ema_fast = pd.Series(pre_prices).ewm(span=30, adjust=False).mean().iloc[-1]
        pre_ema_slow = pd.Series(pre_prices).ewm(span=120, adjust=False).mean().iloc[-1]
        features['pre_ema_regime'] = (pre_ema_fast - pre_ema_slow) / (pre_ema_slow + 1e-9)
    else:
        features['pre_market_trend'] = 0
        features['pre_market_ofi'] = 0
        features['volume_surge'] = 1
        features['pre_ema_regime'] = 0
    
    # ---- 8. TIME FEATURES ----
    features['entry_seconds'] = entry_seconds
    features['remaining_seconds'] = 900 - entry_seconds
    features['pct_elapsed'] = entry_seconds / 900
    
    hour = pd.to_datetime(open_ms, unit='ms').hour
    features['hour_sin'] = np.sin(2 * np.pi * hour / 24)
    features['hour_cos'] = np.cos(2 * np.pi * hour / 24)
    
    return features

# ========================================================================
# BUILD DATASET WITH CROSS-MARKET FEATURES
# ========================================================================

def build_dataset(df_trades, df_markets, entry_seconds_list=[30, 60, 120, 300]):
    """
    Build the full dataset: for each market and entry point, compute features.
    Also adds cross-market features (streak, previous outcomes).
    """
    print(f"\nBuilding features for entry points: {entry_seconds_list} seconds...")
    
    all_rows = []
    
    for entry_secs in entry_seconds_list:
        print(f"\n  Entry at {entry_secs}s:")
        skipped = 0
        
        for i, market in df_markets.iterrows():
            # Compute market-level features
            feats = compute_market_features(df_trades, market, entry_secs)
            if feats is None:
                skipped += 1
                continue
            
            # Cross-market features (previous market outcomes)
            if i >= 1:
                feats['prev_market_result'] = df_markets.iloc[i-1]['resolution_int']
                feats['prev_market_change'] = df_markets.iloc[i-1]['price_change_pct']
            else:
                feats['prev_market_result'] = 0.5
                feats['prev_market_change'] = 0
            
            if i >= 3:
                last_3 = df_markets.iloc[i-3:i]['resolution_int']
                feats['streak_3'] = last_3.sum() - 1.5  # Centered: +1.5 = all UP, -1.5 = all DOWN
                feats['streak_consistency'] = last_3.std()  # 0 = consistent streak
            else:
                feats['streak_3'] = 0
                feats['streak_consistency'] = 0.5
            
            if i >= 6:
                last_6 = df_markets.iloc[i-6:i]['resolution_int']
                feats['trend_6'] = last_6.mean() - 0.5  # >0 = bullish run
            else:
                feats['trend_6'] = 0
            
            # Target
            feats['target'] = market['resolution_int']
            feats['market_idx'] = i
            feats['entry_secs'] = entry_secs
            
            all_rows.append(feats)
        
        print(f"    Generated {len([r for r in all_rows if r.get('entry_secs') == entry_secs])} samples (skipped {skipped})")
    
    df = pd.DataFrame(all_rows)
    print(f"\n  Total dataset: {len(df)} rows, {len(df.columns)} columns")
    return df

# ========================================================================
# ML: FULL STACKING ENSEMBLE
# ========================================================================

def run_ensemble(df, label):
    """Run the full XGB + LGB + Meta stacking ensemble."""
    
    exclude = {'target', 'market_idx', 'entry_secs'}
    feats = [c for c in df.columns if c not in exclude]
    
    X = df[feats].fillna(0).replace([np.inf, -np.inf], 0)
    y = df['target']
    
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  Rows: {len(df)} | Features: {len(feats)}")
    up_pct = y.mean()
    print(f"  Target: UP={up_pct:.1%}, DOWN={1-up_pct:.1%}")
    print(f"{'='*70}")
    
    if len(df) < 50:
        print("  SKIP: Too few rows")
        return None
    
    # Chronological split
    n = len(df)
    tr = int(n * 0.6)
    vl = int(n * 0.8)
    
    Xtr, ytr = X.iloc[:tr], y.iloc[:tr]
    Xvl, yvl = X.iloc[tr:vl], y.iloc[tr:vl]
    Xte, yte = X.iloc[vl:], y.iloc[vl:]
    
    spw = (ytr == 0).sum() / ((ytr == 1).sum() + 1e-9)
    
    # --- XGBoost ---
    xgb_m = xgb.XGBClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01,
        subsample=0.7, colsample_bytree=0.5, gamma=0.5,
        min_child_weight=3, reg_lambda=0.5, scale_pos_weight=spw,
        eval_metric='logloss', random_state=42, early_stopping_rounds=30
    )
    xgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
    
    # --- LightGBM ---
    lgb_m = lgb.LGBMClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01,
        subsample=0.7, colsample_bytree=0.5, min_child_weight=3,
        reg_lambda=0.5, scale_pos_weight=spw, random_state=42,
        verbose=-1, n_jobs=1
    )
    lgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)],
              callbacks=[lgb.early_stopping(30, verbose=False)])
    
    # --- Meta Learner ---
    xgb_vp = xgb_m.predict_proba(Xvl)[:, 1]
    lgb_vp = lgb_m.predict_proba(Xvl)[:, 1]
    meta_X_vl = np.column_stack([xgb_vp, lgb_vp])
    meta_clf = LogisticRegression(C=1.0, random_state=42)
    meta_clf.fit(meta_X_vl, yvl)
    
    # --- Test predictions ---
    xgb_tp = xgb_m.predict_proba(Xte)[:, 1]
    lgb_tp = lgb_m.predict_proba(Xte)[:, 1]
    meta_X_te = np.column_stack([xgb_tp, lgb_tp])
    y_prob = meta_clf.predict_proba(meta_X_te)[:, 1]
    y_pred = (y_prob > 0.5).astype(int)
    
    acc = accuracy_score(yte, y_pred)
    ll = log_loss(yte, y_prob)
    
    print(f"\n  Ensemble Accuracy: {acc:.4f}")
    print(f"  Log Loss:         {ll:.4f}")
    print(f"  XGB iters: {xgb_m.best_iteration}, LGB iters: {lgb_m.best_iteration_}")
    print(classification_report(yte, y_pred, target_names=['DOWN', 'UP'], digits=4))
    
    # Feature importance (from XGBoost)
    imps = pd.Series(xgb_m.feature_importances_, index=feats).sort_values(ascending=False)
    print("  Top 15 Features:")
    for f, v in imps.head(15).items():
        print(f"    {f:30s} {v:.4f}")
    
    # Confidence bands
    print("\n  Confidence Bands (Ensemble):")
    for t in [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70, 0.75, 0.80]:
        mask = (y_prob > t) | (y_prob < (1 - t))
        if mask.sum() >= 3:
            ca = accuracy_score(yte[mask], y_pred[mask])
            n_trades = mask.sum()
            print(f"    >{t:.2f}: Acc={ca:.4f} ({n_trades}/{len(yte)} = {mask.mean():.1%})")
    
    # Drift probability as standalone predictor (sanity check)
    if 'drift_prob_up' in feats:
        drift_pred = (Xte['drift_prob_up'] > 0.5).astype(int)
        drift_acc = accuracy_score(yte, drift_pred)
        print(f"\n  Drift-only baseline: {drift_acc:.4f}")
    
    return {
        'acc': acc, 'log_loss': ll,
        'xgb': xgb_m, 'lgb': lgb_m, 'meta': meta_clf,
        'features': feats, 'y_prob': y_prob, 'y_test': yte
    }

# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    df_trades = load_binance_trades(conn)
    conn.close()
    
    print(f"\nLoaded {len(df_trades):,} Binance trades")
    print(f"Time range: {pd.to_datetime(df_trades['trade_time'].min(), unit='ms')} to {pd.to_datetime(df_trades['trade_time'].max(), unit='ms')}")
    
    # Build 15-minute markets
    df_markets = build_15min_markets(df_trades)
    
    # Test at different entry points
    entry_points = [30, 60, 120, 300]
    
    print(f"\n{'#'*70}")
    print(f"# TESTING ALL ENTRY POINTS COMBINED")
    print(f"{'#'*70}")
    
    # Combined dataset (all entry points)
    df_all = build_dataset(df_trades, df_markets, entry_points)
    result_all = run_ensemble(df_all, "ALL ENTRY POINTS COMBINED")
    
    # Test each entry point individually
    results = {}
    for entry_s in entry_points:
        print(f"\n{'#'*70}")
        print(f"# ENTRY AT {entry_s}s")
        print(f"{'#'*70}")
        
        df_single = build_dataset(df_trades, df_markets, [entry_s])
        r = run_ensemble(df_single, f"Entry at {entry_s}s into market")
        if r:
            results[entry_s] = r
    
    # Summary
    elapsed = time.time() - t0
    print(f"\n\n{'='*70}")
    print(f"  POLYMARKET-ALIGNED RESULTS ({elapsed:.0f}s)")
    print(f"{'='*70}")
    print(f"\n  {'Entry':>8s}  {'Accuracy':>10s}  {'LogLoss':>10s}  {'Drift-Only':>12s}")
    print(f"  {'-'*50}")
    
    for entry_s, r in sorted(results.items()):
        drift_base = ""
        if 'drift_prob_up' in r.get('features', []):
            dp = (r['y_prob'] > 0.5).astype(int)
            drift_base = f"{accuracy_score(r['y_test'], dp):.4f}"
        print(f"  {entry_s:>6d}s  {r['acc']:>10.4f}  {r['log_loss']:>10.4f}  {drift_base:>12s}")
    
    if result_all:
        print(f"\n  Combined:  {result_all['acc']:.4f} accuracy on {len(result_all['y_test'])} test markets")
    
    print(f"\n  Total markets: {len(df_markets)}")
    print(f"  Total elapsed: {elapsed:.0f}s")

if __name__ == "__main__":
    main()
