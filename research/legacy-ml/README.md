# Legacy ML Archive

This directory consolidates the useful source material from the older
`gregyoung14/polymarket-btc-15min-ML` repository.

The active Rust-native backtesting work lives under `strategies/` in the repo
root. This archive is for historical ML research, Python prototypes, feature
engineering experiments, and model-training utilities that predate the current
v8-v15 strategy line.

## Imported

- `strategies/v1_ml_test_bench` through `strategies/v7_drift_refinement`
- `strategies/v9_regime_filter` and `strategies/v9_2_regime_improved`
- Legacy Python utilities for ledger analysis, data fetching, model training,
  SHAP/ensemble analysis, and infrastructure checks
- Original model metadata: `models/features.json` and `models/stats.json`
- The original repository README as `ORIGINAL_README.md`

## Intentionally Omitted

- Generated backtest CSV files
- Generated HTML dashboards
- Rust `Cargo.lock` files from old standalone experiments
- Binary pickle model artifacts (`xgb_model.pkl`, `lgb_model.pkl`,
  `meta_clf.pkl`)
- Duplicate v8, v10, and v11 strategy implementations already represented by
  the active Rust-native strategy tree

If an old binary model artifact is needed again, recover it from the
`polymarket-btc-15min-ML` Git history or retrain it from the archived utilities.
