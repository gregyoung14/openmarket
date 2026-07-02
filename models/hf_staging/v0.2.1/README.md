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

Pretrained model artifacts for the OpenMarket research archive. Model binaries
are published here rather than committed to the OpenMarket GitHub repository.

## How to Cite

```bibtex
@misc{openmarket_models_v02_2026,
  title        = {{OpenMarket} Binary-Outcome Calibration Model},
  author       = {{OpenMarket Contributors}},
  year         = {2026},
  howpublished = {\url{https://huggingface.co/gregyoung14/openmarket-models}},
  note         = {Release v0.2.1}
}
```

## Repository Layout

```text
v0.2.1/                            # current recommended release
  binary_outcome_model.json        # walk-forward logistic scorer (Platt scaling)
  binary_outcome_metrics_*.json    # training metrics + walk-forward windows
  model_manifest.json              # provenance manifest
v0.2/                              # prior full-unified training run
  ...
v0.1/                              # earlier release (smaller training set)
  binary_outcome_model.json
  binary_outcome_metrics_*.json
  model_manifest.json
README.md
```

## Current Release (v0.2.1)

| Artifact | Description |
|---|---|
| `v0.2.1/binary_outcome_model.json` | Calibrated logistic binary-outcome scorer |
| `v0.2.1/binary_outcome_metrics_*.json` | Walk-forward metrics (559 windows) |
| `v0.2.1/model_manifest.json` | Provenance manifest |

**Training pipeline (Rust):**

1. `export_step3_from_parquet` on `unified/` Parquet (`v0.4.3-unified`)
2. `train_binary_outcome_model` — walk-forward logistic regression + Platt scaling

**Training data:**

| Field | Value |
|---|---:|
| Dataset | `gregyoung14/openmarket-btc-polymarket` (`v0.4.3-unified`) |
| Feature export | `step3_binary_calibration` CSV from unified Parquet |
| Rows | 357,390 |
| Markets | 2,251 / 4,450 in `market_meta` (51% write rate) |
| Notes | Backfilled `unified/` synced to HF in `v0.4.3-unified` |
| Date range | 2026-02-12 → 2026-05-14 |
| Features | 43 |

**Aggregate metrics (calibrated, walk-forward OOS):**

| Metric | Value |
|---|---:|
| AUC-ROC | 0.838 |
| Brier | 0.165 |
| ECE | 0.025 |
| Log loss | 0.495 |

**Simulated +EV trading (fee 1%, slippage 0.5%):** 260,617 trades, 49.4% hit
rate, **-0.117 PnL/trade**. Not deployable alpha — research artifact only.

**Known limitations:**

- Step3 export skips markets without sufficient Polymarket ticks or Binance
  trades (~50% of `market_meta` entries).
- Simulated economics are sensitive to fee/slippage assumptions.
- No ongoing model maintenance; frozen at source tag `v0.5.1`.

**Reproduce:**

```bash
cargo build -p step3-parquet-export -p binary-outcome-trainer --release
./target/release/export_step3_from_parquet \
  --parquet-root data/hf_release/unified_parquet \
  --out-dir data/hf_release/features_exports
./target/release/train_binary_outcome_model \
  --input data/hf_release/features_exports/step3_binary_calibration_<ts>.csv \
  --artifact-dir data/ml_artifacts
```

See `scripts/ml/README.md` in the source repo.

## Previous Releases

### v0.2

Same pipeline on pre-backfill unified Parquet (354,684 rows, 2,234 markets).

### v0.1

| Artifact | Description |
|---|---|
| `v0.1/binary_outcome_model.json` | Earlier calibrated logistic scorer |
| `v0.1/binary_outcome_metrics_*.json` | Metrics snapshots |
| `v0.1/model_manifest.json` | Provenance manifest |

Trained on a smaller step3 export. Superseded by `v0.2/` for research use.

## Required Metadata Per Model

- source code commit
- dataset repo and dataset version
- feature schema (see `feature_names` in model JSON)
- training date
- validation split (walk-forward by market)
- metrics and calibration report
- known limitations

OpenMarket is in archival shutdown. Published artifacts are fixed research
outputs, not an ongoing model release cadence.