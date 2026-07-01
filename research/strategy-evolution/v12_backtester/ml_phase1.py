#!/usr/bin/env python3
"""
V12 ML Phase 1 — Walk-Forward Logistic Regression + SHAP Analysis
==================================================================
Loads v12_features.csv (produced by: cargo run --release) and:

  1. Walk-forward CV on 3-feature model (drift / OFI / scoreboard)
     → Honest accuracy estimate, no look-ahead bias
  2. Walk-forward CV on full 7-feature model (+ regime, timing, vol, autocorr)
     → Does adding context features help?
  3. Learned weight comparison vs hand-tuned 0.55 / 0.30 / 0.15
     → What does the data think the weights should be?
  4. Feature importance (coefficient magnitude)
  5. SHAP analysis — which features actually drive wins?
  6. Confidence calibration — is conf=0.70 actually 70% correct?
  7. Regime breakdown — are Trend/Neutral markets really different?
  8. Suggested v12 config with data-driven weights

Usage:
  cd strategies/v12_backtester
  pip install scikit-learn shap pandas numpy
  python3 ml_phase1.py
  # or: python3 ml_phase1.py --features path/to/v12_features.csv
"""

import sys
import argparse
import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, roc_auc_score

try:
    import shap
    HAS_SHAP = True
except ImportError:
    HAS_SHAP = False
    print("⚠  shap not installed — skipping SHAP analysis")
    print("   Install: pip install shap\n")

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

# The 3 signal components — directly comparable to hand-tuned weights
FEATS_3 = ['drift_prob_up', 'ofi_accel_signal', 'scoreboard_signal']

# All 7 features — adds context that the static formula ignores
FEATS_7 = [
    'drift_prob_up', 'ofi_accel_signal', 'scoreboard_signal',
    'path_eff', 'autocorr', 'vol_1s', 'secs_frac',
]

# Current hand-tuned weights (v12 baseline)
HAND_TUNED = {'drift_prob_up': 0.55, 'ofi_accel_signal': 0.30, 'scoreboard_signal': 0.15}

# ─────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────

def load(path='v12_features.csv') -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.sort_values('epoch_s').reset_index(drop=True)
    df['secs_frac'] = df['secs_in'] / 600.0           # timing as 0→1
    df['target']    = df['correct'].astype(int)         # 1=win 0=loss
    return df

# ─────────────────────────────────────────────────────────────────
# WALK-FORWARD CV
# ─────────────────────────────────────────────────────────────────

def walk_forward(df: pd.DataFrame, features: list, n_folds: int = 4, C: float = 0.5) -> list:
    """
    Expanding-window walk-forward cross-validation.
    Fold k trains on first k/(n_folds+1) of data, tests on next slice.
    No data leakage: test window always follows training window.
    """
    n = len(df)
    fold_size = n // (n_folds + 1)
    results = []

    for fold in range(1, n_folds + 1):
        train_end = fold * fold_size
        test_end  = min((fold + 1) * fold_size, n)
        if test_end - train_end < 20:
            continue

        X_tr = df[features].iloc[:train_end].values
        y_tr = df['target'].iloc[:train_end].values
        X_te = df[features].iloc[train_end:test_end].values
        y_te = df['target'].iloc[train_end:test_end].values

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        lr = LogisticRegression(C=C, max_iter=2000, random_state=42)
        lr.fit(X_tr_s, y_tr)

        preds = lr.predict(X_te_s)
        probs = lr.predict_proba(X_te_s)[:, 1]
        acc   = accuracy_score(y_te, preds)
        try:
            auc = roc_auc_score(y_te, probs)
        except ValueError:
            auc = 0.5

        results.append({
            'fold':     fold,
            'train_n':  train_end,
            'test_n':   len(X_te),
            'accuracy': acc,
            'auc':      auc,
            'coefs':    dict(zip(features, lr.coef_[0])),
        })

    return results

# ─────────────────────────────────────────────────────────────────
# FULL-DATA MODEL (for weight comparison + SHAP)
# ─────────────────────────────────────────────────────────────────

def fit_full(df: pd.DataFrame, features: list, C: float = 0.5):
    scaler = StandardScaler()
    X = scaler.fit_transform(df[features].values)
    y = df['target'].values
    lr = LogisticRegression(C=C, max_iter=2000, random_state=42)
    lr.fit(X, y)
    return lr, scaler

# ─────────────────────────────────────────────────────────────────
# PRETTY PRINTING
# ─────────────────────────────────────────────────────────────────

def sep(title='', width=62, char='─'):
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{char * pad} {title} {char * (width - pad - len(title) - 2)}")
    else:
        print(char * width)

def print_wf(results: list, label: str):
    sep(label)
    print(f"  {'Fold':>5}  {'Train N':>8}  {'Test N':>7}  {'Accuracy':>9}  {'AUC':>6}")
    print(f"  {'─'*5}  {'─'*8}  {'─'*7}  {'─'*9}  {'─'*6}")
    for r in results:
        print(f"  {r['fold']:>5}  {r['train_n']:>8}  {r['test_n']:>7}  "
              f"{r['accuracy']:>9.4f}  {r['auc']:>6.4f}")
    accs = [r['accuracy'] for r in results]
    if accs:
        print(f"\n  Mean accuracy: {np.mean(accs):.4f}  "
              f"(±{np.std(accs):.4f} std across folds)")

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--features', default='v12_features.csv',
                        help='Path to v12_features.csv from Rust backtester')
    parser.add_argument('--folds', type=int, default=4,
                        help='Number of walk-forward folds (default: 4)')
    args = parser.parse_args()

    print("═" * 62)
    print(" V12 ML PHASE 1 — LOGISTIC REGRESSION + SHAP")
    print("═" * 62)

    df = load(args.features)
    n   = len(df)
    wr  = df['target'].mean()
    print(f"\n  Loaded {n} baseline signal fires")
    print(f"  Epoch range: {df['epoch_s'].min()} → {df['epoch_s'].max()}")
    print(f"  Overall win rate (training target): {wr:.4f}  ({df['target'].sum()}/{n})")
    print(f"  Walk-forward folds: {args.folds}")

    # ── 1. Walk-forward: 3-feature ──────────────────────────────
    wf3 = walk_forward(df, FEATS_3, n_folds=args.folds)
    print_wf(wf3, "WALK-FORWARD: 3-Feature (drift / OFI / scoreboard)")

    # ── 2. Walk-forward: 7-feature ──────────────────────────────
    wf7 = walk_forward(df, FEATS_7, n_folds=args.folds)
    print_wf(wf7, "WALK-FORWARD: 7-Feature (+ regime / timing / vol / autocorr)")

    # Comparison summary
    acc3 = np.mean([r['accuracy'] for r in wf3]) if wf3 else 0.0
    acc7 = np.mean([r['accuracy'] for r in wf7]) if wf7 else 0.0
    delta = (acc7 - acc3) * 100
    print(f"\n  Adding 4 context features: {delta:+.2f}% accuracy change")
    if delta > 0.5:
        print("  → 7-feature model is meaningfully better")
    elif delta < -0.5:
        print("  → 3-feature model is better (context features add noise)")
    else:
        print("  → No meaningful difference (use 3-feature / cleaner model)")

    # ── 3. Weight comparison ─────────────────────────────────────
    sep("LEARNED WEIGHTS vs HAND-TUNED")
    lr3, sc3 = fit_full(df, FEATS_3)
    raw_coef = lr3.coef_[0]

    # Normalise raw coefficients to sum to 1 for comparison
    pos = np.maximum(raw_coef, 0.0)
    total = pos.sum()
    norm_weights = pos / total if total > 0 else pos

    print(f"\n  {'Feature':<24}  {'Hand-Tuned':>10}  {'Learned':>10}  {'Δ':>8}  Dir")
    print(f"  {'─'*24}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*10}")
    for feat, lw, nw in zip(FEATS_3, [0.55, 0.30, 0.15], norm_weights):
        delta_w = nw - lw
        arrow = "▲ HIGHER" if delta_w > 0.02 else ("▼ lower" if delta_w < -0.02 else "≈ same")
        print(f"  {feat:<24}  {lw:>10.3f}  {nw:>10.3f}  {delta_w:>+8.3f}  {arrow}")

    # Simulated in-sample accuracy with each weight set
    wr_hand = (
        (df[FEATS_3].values @ np.array([0.55, 0.30, 0.15])) > 0.5
    ) == (df['target'].values == 1)
    wr_hand_acc = wr_hand.mean()

    wr_lr_acc = accuracy_score(df['target'], lr3.predict(sc3.transform(df[FEATS_3])))

    print(f"\n  In-sample accuracy (hand-tuned 0.55/0.30/0.15): {wr_hand_acc:.4f}")
    print(f"  In-sample accuracy (learned weights):           {wr_lr_acc:.4f}")
    print(f"  In-sample gain: {(wr_lr_acc - wr_hand_acc)*100:+.2f}%  "
          f"(⚠ in-sample always looks good — see walk-forward above for real estimate)")

    # ── 4. Feature importance: all 7 ────────────────────────────
    sep("FEATURE IMPORTANCE (7-feature model, full data)")
    lr7, sc7 = fit_full(df, FEATS_7)
    coef_abs = np.abs(lr7.coef_[0])
    order = np.argsort(coef_abs)[::-1]

    print(f"\n  {'Rank':<5}  {'Feature':<24}  {'|Coef|':>8}  Effect")
    print(f"  {'─'*5}  {'─'*24}  {'─'*8}  {'─'*30}")
    for rank, i in enumerate(order, 1):
        feat = FEATS_7[i]
        sign = "↑ WIN" if lr7.coef_[0][i] > 0 else "↓ LOSS"
        print(f"  {rank:<5}  {feat:<24}  {coef_abs[i]:>8.4f}  {sign}")

    # ── 5. SHAP ──────────────────────────────────────────────────
    if HAS_SHAP:
        sep("SHAP ANALYSIS (7-feature model)")
        X7 = sc7.transform(df[FEATS_7].values)
        explainer   = shap.LinearExplainer(lr7, X7)
        shap_values = explainer.shap_values(X7)
        mean_abs    = np.abs(shap_values).mean(axis=0)
        shap_order  = np.argsort(mean_abs)[::-1]

        print(f"\n  {'Rank':<5}  {'Feature':<24}  {'Mean |SHAP|':>12}")
        print(f"  {'─'*5}  {'─'*24}  {'─'*12}")
        for rank, i in enumerate(shap_order, 1):
            print(f"  {rank:<5}  {FEATS_7[i]:<24}  {mean_abs[i]:>12.6f}")

        # Save SHAP values
        shap_df = pd.DataFrame(shap_values, columns=FEATS_7)
        shap_df.to_csv('v12_shap_values.csv', index=False)
        print(f"\n  Saved → v12_shap_values.csv")

    # ── 6. Confidence calibration ────────────────────────────────
    sep("CONFIDENCE CALIBRATION")
    print(f"\n  Is conf=0.70 really 70% correct? (perfect calibration = conf≈WR)")
    print(f"\n  {'Conf Bin':<18}  {'Actual WR':>10}  {'Gap':>8}  Count")
    print(f"  {'─'*18}  {'─'*10}  {'─'*8}  {'─'*6}")
    bins = pd.cut(df['confidence'], bins=8)
    cal = df.groupby(bins, observed=True)['target'].agg(['mean', 'count'])
    for idx, row in cal.iterrows():
        if row['count'] < 5:
            continue
        conf_mid = (idx.left + idx.right) / 2
        gap      = row['mean'] - conf_mid
        flag     = " ← overconfident" if gap < -0.05 else (" ← underconfident" if gap > 0.05 else "")
        print(f"  {str(idx):<18}  {row['mean']:>10.3f}  {gap:>+8.3f}  {int(row['count']):>5}{flag}")

    # ── 7. Regime breakdown ──────────────────────────────────────
    sep("WIN RATE BY REGIME")
    print(f"\n  {'Regime':<10}  {'Count':>6}  {'Win Rate':>9}  Δ vs overall")
    print(f"  {'─'*10}  {'─'*6}  {'─'*9}  {'─'*14}")
    regime_map = {0: 'Trend', 1: 'Neutral', 2: 'Chop'}
    for reg_id, reg_name in regime_map.items():
        sub = df[df['regime'] == reg_id]
        if len(sub) == 0:
            continue
        sub_wr = sub['target'].mean()
        delta_reg = sub_wr - wr
        print(f"  {reg_name:<10}  {len(sub):>6}  {sub_wr:>9.4f}  {delta_reg:>+.4f}")

    # ── 8. Suggested v12 config ──────────────────────────────────
    sep("SUGGESTED CONFIG (data-driven weights)")
    print(f"\n  Based on LR fit on {n} signal fires (walk-forward validated):")
    print(f"\n  // In signal-engine/src/config.rs:")

    feat_to_const = {
        'drift_prob_up':     'W_DRIFT',
        'ofi_accel_signal':  'W_OFI_ACCEL',
        'scoreboard_signal': 'W_SCOREBOARD',
    }
    for feat, const in feat_to_const.items():
        idx_ = FEATS_3.index(feat)
        w = float(norm_weights[idx_])
        current = HAND_TUNED[feat]
        changed = " // ← changed" if abs(w - current) > 0.02 else " // unchanged"
        print(f"  pub const {const:<16}: f64 = {w:.3f};{changed}")

    # Walk-forward accuracy as the honest estimate
    best_wf_acc = max(acc3, acc7)
    best_model  = "7-feature" if acc7 > acc3 else "3-feature"
    baseline_wr = wr  # naive "always predict majority class"

    print(f"\n  Walk-forward accuracy ({best_model}): {best_wf_acc:.4f}")
    print(f"  Naive baseline (always-majority):    {max(wr, 1-wr):.4f}")
    print(f"  Lift over naive:                     {(best_wf_acc - max(wr, 1-wr))*100:+.2f}%")

    sep()
    print(" NEXT STEPS")
    sep()
    print("""
  1. If learned weights differ significantly from 0.55/0.30/0.15:
       → Update config.rs with the suggested weights above
       → Re-run v12 backtester to see if directional WR improves

  2. If walk-forward accuracy > naive baseline by >1%:
       → The ML signal is real — consider blending it with the
         statistical signal in the execution engine

  3. Pull 1 year of Binance agg_trades from Binance Vision:
       → data.binance.vision/data/spot/monthly/aggTrades/BTCUSDT/
       → Re-run with 20,000+ samples for reliable ML results

  4. Re-run this script after collecting more Polymarket data:
       → At 30+ days: confidence calibration becomes reliable
       → At 60+ days: regime-conditional weights become testable
""")


if __name__ == '__main__':
    main()
