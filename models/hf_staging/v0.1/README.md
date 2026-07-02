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

## Planned Artifacts

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

## Current Status

Published calibrated binary-outcome scorer (v0.1) trained on OpenMarket step3 features. Paired dataset version: `v0.3-unified`.
