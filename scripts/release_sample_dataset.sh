#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT="${1:-polymarket_btc_data_2026-05-14_145928.db.gz}"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r scripts/datasets/requirements.txt -r scripts/hf/requirements.txt

.venv/bin/python scripts/datasets/export_snapshot_to_parquet.py \
  "$SNAPSHOT" \
  --manifest data/hf_release/metadata/snapshot_manifest.json \
  --out-dir data/hf_release/sample_parquet \
  --staging-dir data/hf_release/staging \
  --chunk-rows 10000

echo "Sample Parquet written to data/hf_release/sample_parquet"
