# GitHub Release Checklist

## v0.1.0

Required before tagging:

- [ ] Push `gregyoung14/openmarket`
- [ ] Create `gregyoung14/openmarket-btc-polymarket`
- [ ] Create `gregyoung14/openmarket-models`
- [ ] Upload dataset card to the dataset repo
- [ ] Upload `sample/` Parquet split
- [ ] Upload snapshot manifest under `metadata/`
- [ ] Validate sample split from a clean clone
- [ ] Run `cargo check --workspace`
- [ ] Run `python3 -m py_compile scripts/datasets/*.py scripts/hf/*.py`
- [ ] Record benchmark baseline
- [ ] Decide whether pretrained models ship in v0.1.0 or remain deferred

Release metadata:

```text
Source tag: v0.1.0
Dataset: huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
Dataset version: v0.1-sample
Models: huggingface.co/gregyoung14/openmarket-models
Model version: none / deferred
Paper: paper/paper.md
```

Post-release:

- [ ] Open issues for full Parquet export
- [ ] Open issues for merge/dedupe/validation scripts
- [ ] Open issues for first benchmark table
- [ ] Open issues for example notebooks
