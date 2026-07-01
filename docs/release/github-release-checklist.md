# GitHub Release Checklist

## v0.1.0

Required before tagging:

- [x] Push `gregyoung14/openmarket`
- [x] Create `gregyoung14/openmarket-btc-polymarket`
- [x] Create `gregyoung14/openmarket-models`
- [x] Upload dataset card to the dataset repo
- [x] Upload `sample/` Parquet split
- [x] Upload snapshot manifest under `metadata/`
- [x] Validate sample split from a clean clone (`scripts/hf/validate_sample_split.py` -> PASS, 12 tables OK)
- [x] Run `cargo check --workspace` (0 errors, 16 dead-code warnings)
- [x] Run `python3 -m py_compile scripts/datasets/*.py scripts/hf/*.py` (all compile clean)
- [x] Record benchmark baseline (`benchmarks/baselines/v0.1-sample.{json,md}`)
- [x] Decide whether pretrained models ship in v0.1.0 or remain deferred

Benchmark baseline (`v0.1-sample`):

```text
download_seconds: 1.551
load_seconds:     0.002
total_rows:       9,352
parquet_bytes:    204,401
tables:           12 (binance_trades, binance_ticks_ms, binance_candles_1s/5s/1m/5m/15m/1h,
                   polymarket_ticks_ms, lag_pairs_ms, market_meta)
```

OpenMarket GitHub push verification:

```bash
gh api repos/gregyoung14/openmarket --jq '.visibility, .pushed_at'
# private, 2026-07-01T04:17:11Z
```

Release metadata:

```text
Source tag: v0.1.0
Dataset: huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version: v0.1-sample
Models: huggingface.co/gregyoung14/openmarket-models
Model version: deferred; model-card scaffold uploaded
Paper: paper/paper.md
```

Hugging Face upload verification:

```bash
hf download gregyoung14/openmarket-btc-polymarket --repo-type dataset --dry-run
# 19 files, 370.7 KB

hf download gregyoung14/openmarket-models --repo-type model --dry-run
# 2 files, 2.3 KB
```

Post-release:

- [ ] Open issues for full Parquet export
- [ ] Open issues for merge/dedupe/validation scripts
- [ ] Open issues for first benchmark table
- [ ] Open issues for example notebooks
