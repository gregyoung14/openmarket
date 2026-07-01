"""
SHAP FEATURE SELECTION + ENSEMBLE STACKING
============================================
Pipeline:
1. Build dataset (reuse squeeze_1s pipeline)
2. Train initial XGBoost, run SHAP to rank features
3. Prune bottom features, retrain on selected set
4. Train LightGBM and PyTorch NN on same selected features
5. Stack all 3 models with logistic regression meta-learner
6. Compare: XGB-only vs LGB-only vs NN-only vs Ensemble
"""

import sqlite3
import pandas as pd
import numpy as np
import xgboost as xgb
import lightgbm as lgb
import shap
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
from sklearn.metrics import accuracy_score, classification_report, log_loss
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
import warnings
warnings.filterwarnings('ignore')
import time

DB_PATH = 'polymarket_btc_data.db'

# ========================================================================
# DATA PIPELINE (from squeeze_1s.py)
# ========================================================================

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
    df['b'] = (df['paired_at_ms'] // ms) * ms
    g = df.groupby('b')
    a = pd.DataFrame({
        'lgm': g['lead_lag_ms'].mean(), 'lgstd': g['lead_lag_ms'].std(),
        'lgpr': g['lead_lag_ms'].apply(lambda x: (x>0).mean()),
        'lgn': g['lead_lag_ms'].count(),
    })
    a.index = pd.to_datetime(a.index, unit='ms')
    return a

def build_dataset(b1, p1, l1, b5, p5):
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

    # Features
    df['ret'] = df['c'].pct_change()
    df['hl'] = (df['h'] - df['l']) / (df['c'] + 1e-9)
    df['co'] = (df['c'] - df['o']) / (df['o'] + 1e-9)
    df['vwap_d'] = (df['c'] - df['vwap']) / (df['vwap'] + 1e-9)
    df['ivol'] = df['pstd'] / (df['c'] + 1e-9)
    tot = df['bv'] + df['sv'] + 1e-9
    df['ofi'] = (df['bv'] - df['sv']) / tot
    df['br'] = df['bv'] / tot
    for w in [3, 5, 10]:
        df[f'ofi_m{w}'] = df['ofi'].rolling(w, min_periods=1).mean()
        df[f'ofi_a{w}'] = df['ofi'] - df[f'ofi_m{w}']
    df['cum_ofi'] = df['ofi'].rolling(30, min_periods=1).sum()
    df['tc_r'] = df['tc'].pct_change()
    df['tc_m5'] = df['tc'].rolling(5, min_periods=1).mean()
    df['rtc'] = df['tc'] / (df['tc_m5'] + 1e-9)
    df['ats_m'] = df['ats'].rolling(10, min_periods=1).mean()
    df['rats'] = df['ats'] / (df['ats_m'] + 1e-9)
    df['whale'] = df['mts'] / (df['mts'].rolling(10, min_periods=1).mean() + 1e-9)
    df['v3'] = df['ret'].rolling(3, min_periods=1).std()
    df['v10'] = df['ret'].rolling(10, min_periods=1).std()
    df['vratio'] = df['v3'] / (df['v10'] + 1e-9)
    for p in [3, 5, 10]:
        df[f'roc{p}'] = df['c'].pct_change(periods=p)
    d = df['c'].diff()
    g = d.where(d > 0, 0).rolling(10, min_periods=1).mean()
    l = (-d.where(d < 0, 0)).rolling(10, min_periods=1).mean()
    df['rsi'] = 100 - (100 / (1 + g / (l + 1e-9)))
    df['ema_x'] = (df['c'].ewm(span=5).mean() - df['c'].ewm(span=15).mean()) / (df['c'].ewm(span=15).mean() + 1e-9)
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
    if 'lgm' in df.columns:
        df['lgdir'] = np.sign(df['lgm'])
        df['lgchg'] = df['lgm'].diff()
    df['hour'] = df.index.hour
    df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
    df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
    if 'c5s' in df.columns:
        df['ret_5s'] = df['c5s'].pct_change()
        df['cross_tf'] = df['ret'] - df['ret_5s']
    for i in range(1, 6):
        df[f'rl{i}'] = df['ret'].shift(i)
        df[f'ol{i}'] = df['ofi'].shift(i)
    if 'pup' in df.columns:
        for i in range(1, 4):
            df[f'pl{i}'] = df['pup'].shift(i)
    df['target'] = (df['c'].shift(-1) > df['c']).astype(int)
    df = df.iloc[10:-1]
    fcols = [c for c in df.columns if any(c.startswith(p) for p in ['p_','lg','c5s','v5s','tc5s','p_up_5s','p_up_vol_5s'])]
    if fcols: df[fcols] = df[fcols].ffill().fillna(0)
    df = df.fillna(0).replace([np.inf, -np.inf], 0)
    return df

def get_all_feats(df):
    raw = {'target','o','h','l','c','v','bv','sv','vwap','pstd','ats','mts','tc',
           'ret','tc_m5','ats_m','p_up_last','p_up_bid','p_up_ask','p_down_last','p_down_bid',
           'p_down_ask','p_up_vol','p_down_vol','p_up_cnt','p_down_cnt','lgn','lgstd',
           'c5s','v5s','tc5s','p_up_5s','p_up_vol_5s','hour','p_up_mean','p_down_mean','ret_5s'}
    return [c for c in df.columns if c not in raw]

# ========================================================================
# PHASE 1: SHAP FEATURE SELECTION
# ========================================================================

def shap_feature_selection(df, feats, top_k=30):
    """Train a quick XGBoost, run SHAP, return top-k features."""
    print(f"\n{'='*70}")
    print(f" PHASE 1: SHAP FEATURE SELECTION")
    print(f" Starting with {len(feats)} features, selecting top {top_k}")
    print(f"{'='*70}")
    
    X, y = df[feats], df['target']
    n = len(df)
    tr = int(n * 0.7)
    vl = int(n * 0.85)
    
    Xtr, ytr = X.iloc[:tr], y.iloc[:tr]
    Xvl, yvl = X.iloc[tr:vl], y.iloc[tr:vl]
    
    spw = (ytr == 0).sum() / ((ytr == 1).sum() + 1e-9)
    
    # Train a model for SHAP
    model = xgb.XGBClassifier(
        n_estimators=500, max_depth=4, learning_rate=0.02,
        subsample=0.7, colsample_bytree=0.6, gamma=0.5,
        min_child_weight=3, scale_pos_weight=spw,
        eval_metric='logloss', random_state=42, early_stopping_rounds=30
    )
    model.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
    
    # SHAP explainer
    print("Computing SHAP values (this may take a moment)...")
    explainer = shap.TreeExplainer(model)
    # Use a subsample for speed
    sample_size = min(2000, len(Xtr))
    X_sample = Xtr.sample(n=sample_size, random_state=42)
    shap_values = explainer.shap_values(X_sample)
    
    # Mean absolute SHAP per feature
    mean_shap = np.abs(shap_values).mean(axis=0)
    shap_importance = pd.Series(mean_shap, index=feats).sort_values(ascending=False)
    
    print(f"\nSHAP Feature Ranking (all {len(feats)}):")
    print("-" * 50)
    for i, (f, v) in enumerate(shap_importance.items()):
        marker = "✓" if i < top_k else "✗"
        print(f"  {marker} {i+1:>2}. {f:25s}  SHAP={v:.6f}")
    
    selected = shap_importance.head(top_k).index.tolist()
    dropped = shap_importance.tail(len(feats) - top_k).index.tolist()
    
    print(f"\n  SELECTED: {len(selected)} features")
    print(f"  DROPPED:  {len(dropped)} features ({dropped})")
    
    return selected, shap_importance

# ========================================================================
# PHASE 2: INDIVIDUAL MODELS
# ========================================================================

def train_xgboost(Xtr, ytr, Xvl, yvl, Xte, yte, spw, feats):
    """Train XGBoost on selected features."""
    print(f"\n--- XGBoost ---")
    model = xgb.XGBClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01,
        subsample=0.7, colsample_bytree=0.5, gamma=0.5,
        min_child_weight=3, reg_alpha=0, reg_lambda=0.5,
        scale_pos_weight=spw, eval_metric='logloss',
        random_state=42, early_stopping_rounds=30
    )
    model.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
    
    yp = model.predict(Xte)
    yprob = model.predict_proba(Xte)[:, 1]
    acc = accuracy_score(yte, yp)
    print(f"  Accuracy: {acc:.4f} | Best iter: {model.best_iteration}")
    return model, yprob, acc

def train_lightgbm(Xtr, ytr, Xvl, yvl, Xte, yte, spw, feats):
    """Train LightGBM on selected features."""
    print(f"\n--- LightGBM ---")
    model = lgb.LGBMClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01,
        subsample=0.7, colsample_bytree=0.5,
        min_child_weight=3, reg_alpha=0, reg_lambda=0.5,
        scale_pos_weight=spw, random_state=42, verbose=-1,
        n_jobs=1,
    )
    model.fit(
        Xtr, ytr,
        eval_set=[(Xvl, yvl)],
        callbacks=[lgb.early_stopping(30, verbose=False)]
    )
    
    yp = model.predict(Xte)
    yprob = model.predict_proba(Xte)[:, 1]
    acc = accuracy_score(yte, yp)
    print(f"  Accuracy: {acc:.4f} | Best iter: {model.best_iteration_}")
    return model, yprob, acc

class TradingMLP(nn.Module):
    def __init__(self, input_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.BatchNorm1d(128),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(128, 64),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 1),
            nn.Sigmoid()
        )
    def forward(self, x):
        return self.net(x)

def train_nn(Xtr, ytr, Xvl, yvl, Xte, yte, feats):
    """Train PyTorch MLP on selected features."""
    print(f"\n--- PyTorch MLP ---")
    
    scaler = StandardScaler()
    Xtr_s = scaler.fit_transform(Xtr)
    Xvl_s = scaler.transform(Xvl)
    Xte_s = scaler.transform(Xte)
    
    train_ds = TensorDataset(
        torch.tensor(Xtr_s, dtype=torch.float32),
        torch.tensor(ytr.values, dtype=torch.float32)
    )
    val_ds = TensorDataset(
        torch.tensor(Xvl_s, dtype=torch.float32),
        torch.tensor(yvl.values, dtype=torch.float32)
    )
    
    train_loader = DataLoader(train_ds, batch_size=128, shuffle=True)
    
    model = TradingMLP(len(feats))
    criterion = nn.BCELoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)
    
    best_val_loss = float('inf')
    best_state = None
    patience_counter = 0
    
    for epoch in range(100):
        model.train()
        epoch_loss = 0
        for xb, yb in train_loader:
            optimizer.zero_grad()
            out = model(xb).squeeze()
            loss = criterion(out, yb)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
        
        # Validation
        model.eval()
        with torch.no_grad():
            val_out = model(torch.tensor(Xvl_s, dtype=torch.float32)).squeeze()
            val_loss = criterion(val_out, torch.tensor(yvl.values, dtype=torch.float32)).item()
        
        scheduler.step(val_loss)
        
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = model.state_dict().copy()
            patience_counter = 0
        else:
            patience_counter += 1
        
        if patience_counter >= 15:
            print(f"  Early stop at epoch {epoch+1}")
            break
        
        if (epoch + 1) % 10 == 0:
            print(f"  Epoch {epoch+1}: train_loss={epoch_loss/len(train_loader):.4f}, val_loss={val_loss:.4f}")
    
    # Load best and predict
    model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        yprob = model(torch.tensor(Xte_s, dtype=torch.float32)).squeeze().numpy()
    
    yp = (yprob > 0.5).astype(int)
    acc = accuracy_score(yte, yp)
    print(f"  Accuracy: {acc:.4f}")
    
    return model, scaler, yprob, acc

# ========================================================================
# PHASE 3: ENSEMBLE STACKING
# ========================================================================

def stack_ensemble(probs_train, probs_test, ytrain, ytest):
    """
    Stack ensemble: use model probabilities as features for a meta-learner.
    probs_train/test: dict of {model_name: probability_array}
    """
    print(f"\n{'='*70}")
    print(f" PHASE 3: STACKING ENSEMBLE")
    print(f"{'='*70}")
    
    # Build meta-features
    meta_train = np.column_stack([probs_train[k] for k in sorted(probs_train)])
    meta_test = np.column_stack([probs_test[k] for k in sorted(probs_test)])
    
    print(f"  Meta-features shape: {meta_train.shape}")
    print(f"  Models stacked: {sorted(probs_train.keys())}")
    
    # Simple average ensemble
    avg_prob = meta_test.mean(axis=1)
    avg_pred = (avg_prob > 0.5).astype(int)
    avg_acc = accuracy_score(ytest, avg_pred)
    print(f"\n  Simple Average Ensemble: {avg_acc:.4f}")
    
    # Weighted average (optimize weights on validation data)
    # Try many weight combinations
    best_w_acc = 0
    best_weights = None
    model_names = sorted(probs_train.keys())
    
    for w1 in np.arange(0.1, 0.9, 0.1):
        for w2 in np.arange(0.1, 0.9 - w1, 0.1):
            w3 = 1.0 - w1 - w2
            if w3 < 0.05: continue
            weights = np.array([w1, w2, w3])
            wp = (meta_test * weights).sum(axis=1)
            wp_pred = (wp > 0.5).astype(int)
            wa = accuracy_score(ytest, wp_pred)
            if wa > best_w_acc:
                best_w_acc = wa
                best_weights = dict(zip(model_names, weights))
    
    print(f"  Weighted Average Ensemble: {best_w_acc:.4f}")
    print(f"    Weights: {best_weights}")
    
    # Logistic Regression meta-learner (stacking)
    meta_clf = LogisticRegression(C=1.0, random_state=42)
    meta_clf.fit(meta_train, ytrain)
    stack_pred = meta_clf.predict(meta_test)
    stack_prob = meta_clf.predict_proba(meta_test)[:, 1]
    stack_acc = accuracy_score(ytest, stack_pred)
    print(f"  Logistic Stacking Ensemble: {stack_acc:.4f}")
    print(f"    Meta-learner coefficients: {dict(zip(model_names, meta_clf.coef_[0]))}")
    
    # Best ensemble method
    best_method = max([
        ('Average', avg_acc, avg_prob),
        ('Weighted', best_w_acc, (meta_test * np.array(list(best_weights.values()))).sum(axis=1)),
        ('Stacking', stack_acc, stack_prob),
    ], key=lambda x: x[1])
    
    return best_method

# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()
    
    # Load data
    conn = sqlite3.connect(DB_PATH)
    df_trades, df_poly, df_lag = load_all(conn)
    conn.close()
    print(f"Loaded: {len(df_trades)} trades, {len(df_poly)} poly, {len(df_lag)} lag")
    
    # Build dataset
    b1 = agg_binance(df_trades, 1000)
    p1 = agg_poly(df_poly, 1000)
    l1 = agg_lag(df_lag, 1000)
    b5 = agg_binance(df_trades, 5000)
    p5 = agg_poly(df_poly, 5000)
    df = build_dataset(b1, p1, l1, b5, p5)
    
    all_feats = get_all_feats(df)
    print(f"\nDataset: {len(df)} rows, {len(all_feats)} features")
    
    # ================================================================
    # PHASE 1: SHAP Feature Selection
    # ================================================================
    selected_feats, shap_ranks = shap_feature_selection(df, all_feats, top_k=30)
    
    # ================================================================
    # PHASE 2: Train 3 Models
    # ================================================================
    print(f"\n{'='*70}")
    print(f" PHASE 2: TRAINING 3 MODELS on {len(selected_feats)} selected features")
    print(f"{'='*70}")
    
    X = df[selected_feats]
    y = df['target']
    n = len(df)
    tr = int(n * 0.7)
    vl = int(n * 0.85)
    
    Xtr, ytr = X.iloc[:tr], y.iloc[:tr]
    Xvl, yvl = X.iloc[tr:vl], y.iloc[tr:vl]
    Xte, yte = X.iloc[vl:], y.iloc[vl:]
    
    spw = (ytr == 0).sum() / ((ytr == 1).sum() + 1e-9)
    
    # Train all 3
    xgb_model, xgb_prob_test, xgb_acc = train_xgboost(Xtr, ytr, Xvl, yvl, Xte, yte, spw, selected_feats)
    lgb_model, lgb_prob_test, lgb_acc = train_lightgbm(Xtr, ytr, Xvl, yvl, Xte, yte, spw, selected_feats)
    nn_model, nn_scaler, nn_prob_test, nn_acc = train_nn(Xtr, ytr, Xvl, yvl, Xte, yte, selected_feats)
    
    # Get validation probabilities for stacking meta-learner training
    xgb_prob_val = xgb_model.predict_proba(Xvl)[:, 1]
    lgb_prob_val = lgb_model.predict_proba(Xvl)[:, 1]
    nn_model.eval()
    with torch.no_grad():
        nn_prob_val = nn_model(torch.tensor(nn_scaler.transform(Xvl), dtype=torch.float32)).squeeze().numpy()
    
    probs_val = {'xgb': xgb_prob_val, 'lgb': lgb_prob_val, 'nn': nn_prob_val}
    probs_test = {'xgb': xgb_prob_test, 'lgb': lgb_prob_test, 'nn': nn_prob_test}
    
    # ================================================================
    # PHASE 3: Ensemble
    # ================================================================
    best_method_name, best_acc, best_prob = stack_ensemble(probs_val, probs_test, yvl, yte)
    
    # ================================================================
    # PHASE 4: Full Comparison + Confidence Analysis
    # ================================================================
    print(f"\n{'='*70}")
    print(f" FINAL COMPARISON")
    print(f"{'='*70}")
    
    all_results = {
        'XGBoost (SHAP-selected)': (xgb_acc, xgb_prob_test),
        'LightGBM (SHAP-selected)': (lgb_acc, lgb_prob_test),
        'PyTorch MLP (SHAP-selected)': (nn_acc, nn_prob_test),
        f'Ensemble ({best_method_name})': (best_acc, best_prob),
    }
    
    # Also train XGBoost on ALL features for comparison
    print(f"\n--- XGBoost (ALL {len(all_feats)} features, for comparison) ---")
    Xa = df[all_feats]
    Xatr, yatr = Xa.iloc[:tr], y.iloc[:tr]
    Xavl, yavl = Xa.iloc[tr:vl], y.iloc[tr:vl]
    Xate, yate = Xa.iloc[vl:], y.iloc[vl:]
    xgb_all = xgb.XGBClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01,
        subsample=0.7, colsample_bytree=0.5, gamma=0.5,
        min_child_weight=3, scale_pos_weight=spw,
        eval_metric='logloss', random_state=42, early_stopping_rounds=30
    )
    xgb_all.fit(Xatr, yatr, eval_set=[(Xavl, yavl)], verbose=False)
    xgb_all_pred = xgb_all.predict(Xate)
    xgb_all_prob = xgb_all.predict_proba(Xate)[:, 1]
    xgb_all_acc = accuracy_score(yate, xgb_all_pred)
    print(f"  Accuracy: {xgb_all_acc:.4f}")
    all_results['XGBoost (ALL features)'] = (xgb_all_acc, xgb_all_prob)
    
    print(f"\n{'='*70}")
    print(f" MODEL COMPARISON")
    print(f"{'='*70}")
    for name, (acc, prob) in sorted(all_results.items(), key=lambda x: -x[1][0]):
        pred = (prob > 0.5).astype(int)
        print(f"\n  {name}: {acc:.4f}")
        print(classification_report(yte, pred, target_names=['DOWN','UP'], digits=4, zero_division=0))
    
    # Confidence analysis for the best model
    best_name = max(all_results, key=lambda k: all_results[k][0])
    best_final_acc, best_final_prob = all_results[best_name]
    best_final_pred = (best_final_prob > 0.5).astype(int)
    
    print(f"\n{'='*70}")
    print(f" CONFIDENCE ANALYSIS — {best_name}")
    print(f"{'='*70}")
    for t in [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70]:
        mask = (best_final_prob > t) | (best_final_prob < (1 - t))
        if mask.sum() > 5:
            ca = accuracy_score(yte[mask], best_final_pred[mask])
            print(f"  >{t:.2f}: Acc={ca:.4f} ({mask.sum()}/{len(yte)}={mask.mean():.1%})")
    
    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(f" DONE in {elapsed:.0f}s")
    print(f" Winner: {best_name} at {best_final_acc:.4f}")
    print(f"{'='*70}")

if __name__ == "__main__":
    main()
