# Machine Learning Pipeline

```text
Raw Data
  |
  v
Cleaning
  |
  v
Synchronization
  |
  v
Feature Engineering
  |
  v
Technical Indicators
  |
  v
Training
  |
  v
Validation
  |
  v
Inference
  |
  v
Backtest
  |
  v
Evaluation
```

## Labels

For BTC 15-minute binary markets, labels are generated from market resolution:

- `UP` if BTC settlement price is greater than the market start/reference price
- `DOWN` otherwise

The paper and dataset card should specify the exact settlement source and
whether the first post-window trade, VWAP, or exchange candle close is used.

## Feature Families

Order book:

- spread
- best bid / ask
- microprice
- imbalance
- depth
- liquidity changes

Price:

- returns
- volatility
- momentum
- VWAP deviation
- realized variance

Technical indicators:

- RSI
- EMA
- VWAP
- ATR
- Bollinger Bands
- MACD
- ADX

Custom signals:

- drift score
- order-flow acceleration
- scoreboard signal
- whipsaw/chop detector
- Brier calibration monitor
- confidence-bin empirical edge

## Model Families

The legacy archive includes prototypes for XGBoost, LightGBM, logistic
meta-classifiers, SHAP feature analysis, and stacked ensembles. Public model
artifacts should be released separately from Git.
