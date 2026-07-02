# ML pipeline (Rust)

## 1. Export step3 features from unified Parquet

```bash
cargo build -p step3-parquet-export --release

# full unified timeline (~78s on M5 Max, ~355k rows)
./target/release/export_step3_from_parquet \
  --parquet-root data/hf_release/unified_parquet \
  --out-dir data/hf_release/features_exports

# smoke test
./target/release/export_step3_from_parquet --market-limit 100

# coverage audit — why markets are skipped (writes JSON report, no CSV)
./target/release/export_step3_from_parquet --audit \
  --parquet-root data/hf_release/unified_parquet \
  --out-dir data/hf_release/features_exports
```

Python wrapper (optional): `.venv/bin/python scripts/ml/export_step3_from_parquet.py`

## 2. Train binary outcome model

```bash
cargo build -p binary-outcome-trainer --release

./target/release/train_binary_outcome_model \
  --input data/hf_release/features_exports/step3_binary_calibration_<ts>.csv \
  --artifact-dir data/ml_artifacts
```

~67s on M5 Max for 355k rows / 555 walk-forward windows (parallel Rayon).

Artifacts: `data/ml_artifacts/latest_binary_model.json` + timestamped metrics JSON.

## 0. Backfill unified Parquet (optional)

If step3 audit shows missing partitions or stale merges:

```bash
cargo build -p unified-backfill --release

# Scan row deltas between full_parquet and unified_parquet
./target/release/unified-backfill scan

# Fill missing calendar partitions from staging SQLite archives
./target/release/unified-backfill --jobs 2 sqlite-fill --auto

# Re-merge duplicate shards (usually redundant; dedupe keeps one copy)
./target/release/unified-backfill --jobs 3 repair --tables binance_trades,polymarket_ticks_ms
```

## Legacy SQLite export

Per-snapshot `ml_export` via SQLite is slow on large `.db` files. Prefer Parquet step3 above.
See `scripts/datasets/export_ml_features.py` only if you need archive DB replay.