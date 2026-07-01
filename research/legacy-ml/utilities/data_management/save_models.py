"""
SAVE TRAINED MODELS FOR LIVE INFERENCE
=======================================
Trains the ensemble on all available data and saves:
- XGBoost model
- LightGBM model
- Logistic meta-learner
- Feature list
- Scaler params
"""

import sqlite3
import pandas as pd
import numpy as np
import xgboost as xgb
import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
import joblib
import json
import time

DB_PATH = 'polymarket_btc_data.db'

# Import all the aggregation/feature functions from our pipeline
from squeeze_1s import (
    load_all, agg_binance, agg_poly, agg_lag, build, get_feats
)

def main():
    t0 = time.time()
    
    conn = sqlite3.connect(DB_PATH)
    df_trades, df_poly, df_lag = load_all(conn)
    conn.close()
    print(f"Loaded: {len(df_trades)} trades, {len(df_poly)} poly, {len(df_lag)} lag")
    
    # Build same 1s dataset
    b1 = agg_binance(df_trades, 1000)
    p1 = agg_poly(df_poly, 1000)
    l1 = agg_lag(df_lag, 1000)
    b5 = agg_binance(df_trades, 5000)
    p5 = agg_poly(df_poly, 5000)
    df = build(b1, p1, l1, b5, p5)
    
    feats = get_feats(df)
    X, y = df[feats], df['target']
    n = len(df)
    tr = int(n * 0.7)
    vl = int(n * 0.85)
    
    Xtr, ytr = X.iloc[:tr], y.iloc[:tr]
    Xvl, yvl = X.iloc[tr:vl], y.iloc[tr:vl]
    spw = (ytr==0).sum()/((ytr==1).sum()+1e-9)
    
    print(f"Dataset: {n} rows, {len(feats)} features")
    print(f"Training XGBoost...")
    xgb_m = xgb.XGBClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01, subsample=0.7,
        colsample_bytree=0.5, gamma=0.5, min_child_weight=3, reg_alpha=0,
        reg_lambda=0.5, scale_pos_weight=spw, eval_metric='logloss',
        random_state=42, early_stopping_rounds=30)
    xgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=False)
    print(f"  Best iter: {xgb_m.best_iteration}")
    
    print(f"Training LightGBM...")
    lgb_m = lgb.LGBMClassifier(
        n_estimators=2000, max_depth=4, learning_rate=0.01, subsample=0.7,
        colsample_bytree=0.5, min_child_weight=3, reg_alpha=0, reg_lambda=0.5,
        scale_pos_weight=spw, random_state=42, verbose=-1, n_jobs=1)
    lgb_m.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], callbacks=[lgb.early_stopping(30, verbose=False)])
    print(f"  Best iter: {lgb_m.best_iteration_}")
    
    print(f"Training meta-learner...")
    xgb_vp = xgb_m.predict_proba(Xvl)[:,1]
    lgb_vp = lgb_m.predict_proba(Xvl)[:,1]
    meta_X = np.column_stack([lgb_vp, xgb_vp])
    meta_clf = LogisticRegression(C=1.0, random_state=42)
    meta_clf.fit(meta_X, yvl)
    
    # Save everything
    import os
    os.makedirs('models', exist_ok=True)
    
    joblib.dump(xgb_m, 'models/xgb_model.pkl')
    joblib.dump(lgb_m, 'models/lgb_model.pkl')
    joblib.dump(meta_clf, 'models/meta_clf.pkl')
    
    with open('models/features.json', 'w') as f:
        json.dump(feats, f)
    
    # Save some reference stats for feature normalization
    stats = {
        'feature_means': X[feats].mean().to_dict(),
        'feature_stds': X[feats].std().to_dict(),
        'scale_pos_weight': spw,
        'n_features': len(feats),
        'n_train_rows': len(Xtr),
        'trained_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open('models/stats.json', 'w') as f:
        json.dump(stats, f, indent=2, default=str)
    
    elapsed = time.time() - t0
    print(f"\nModels saved to ./models/ in {elapsed:.0f}s")
    print(f"Features: {feats}")

if __name__ == "__main__":
    main()
