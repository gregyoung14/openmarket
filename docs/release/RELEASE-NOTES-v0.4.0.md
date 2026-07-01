# OpenMarket v0.4.0 — ML features, model weights, snapshot recovery

v0.4.0 completes the deferred release items from v0.3.0: ML feature exports,
published model artifacts, and tooling to recover/re-export corrupt snapshots.

## What changed

- **Dataset**: new `features/` split on Hugging Face with step2 (100ms/1s) and
  step3 (binary calibration) Parquet exports.
- **Models**: published `v0.1/` calibrated binary-outcome scorer to
  `gregyoung14/openmarket-models`.
- **Tools**: `ml_export` Rust binary for offline archive feature generation.
- **Tools**: `export_ml_features.py`, `recover_snapshot.py`,
  `reexport_corrupt_snapshots.py`.
- **Recovery**: re-export pass for the four quarantined partial snapshots.

## Release artifacts

```text
Source tag:        v0.4.0
Dataset:           huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version:   v0.4-features
Models:            huggingface.co/gregyoung14/openmarket-models
Model version:     v0.1
```