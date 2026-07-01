---
license: apache-2.0
library_name: openmarket
tags:
  - prediction-markets
  - polymarket
  - binance
  - bitcoin
  - calibration
  - market-microstructure
---

# OpenMarket Models

This repository is reserved for OpenMarket pretrained models, calibration
artifacts, feature schemas, and model cards.

Model binaries should be uploaded here rather than committed to the OpenMarket
GitHub repository.

## Repository Layout

```text
v0.1/
  feature_schema.json
  training_config.json
  metrics.json
  calibration_report.json
  model.*
```

## Required Metadata Per Model

- source code commit
- dataset repo and dataset version
- feature schema hash
- training date
- validation split
- random seed
- metrics
- calibration report
- known limitations

## Current Release

| Artifact | Version | Description |
|---|---|---|
| `v0.1/binary_outcome_model.json` | v0.1 | Calibrated logistic binary-outcome scorer (Platt scaling) |
| `v0.1/binary_outcome_metrics_*.json` | v0.1 | Training/validation metrics snapshots |
| `v0.1/model_manifest.json` | v0.1 | Provenance manifest |

Paired dataset: `gregyoung14/openmarket-btc-polymarket` at `v0.4-unified`.
Feature schema: 43 columns (see `binary_outcome_model.json` → `feature_names`).

OpenMarket is in archival shutdown. Model artifacts already published here are
intended as fixed research outputs rather than the start of an ongoing model
release cadence.
