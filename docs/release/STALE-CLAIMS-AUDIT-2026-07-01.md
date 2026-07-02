# Stale Claims Audit — 2026-07-01 (resolved, re-checked)

Re-checked after v0.2.1 model upload, unified backfill (`v0.4.3-unified`), and doc sync.

## Ground Truth (2026-07-02)

### Hugging Face dataset `gregyoung14/openmarket-btc-polymarket`

| Artifact | Count / version |
|---|---|
| `v0.1-sample` | 12 flat `*.parquet` at **repo root** (not `sample/`) |
| `full/` | 3,312 parquet, 202 snapshots (`v0.2-full`) |
| `unified/` | 504 parquet, ~727M rows (`v0.4.3-unified`) |
| `features/` | 2 parquet (`v0.4-features`, optional demo; reproducible from `unified/`) |
| `metadata/` | manifest + export reports |

### Hugging Face models `gregyoung14/openmarket-models`

| Version | Status |
|---|---|
| `v0.2.1/` | **Recommended.** 357k rows, 559 walk-forward windows, unified step3 |
| `v0.2/` | Prior release. 354k rows, 555 windows |
| `v0.1/` | Historical comparison artifact |

### Project status

- Archival shutdown; 202 snapshots published-clean
- Full-archive `features/` HF upload is optional (not required; reproducible from `unified/`)

## Doc fixes in this pass

| Location | Issue | Fix |
|---|---|---|
| `datasets/hf/README.md` | Said `sample/` subdirectory | Root flat parquet layout |
| `README.md`, `datasets/README.md` | Same | Root flat + download patterns |
| `datasets/hf/README.md` | Model v0.1 only | v0.2 recommended |
| `paper/paper.md`, tex | v0.1 only | v0.2 + Rust trainer crates |
| `LAUNCH-POST.md` | v0.1 models | v0.2 metrics |
| `PROJECT-STATUS.md` | Missing unified parquet count | 504 parquet files |

## Still optional (not stale — genuinely pending)

- Optional: upload full-archive `features/` to HF (convenience only)
- Optional: migrate `v0.1-sample` from flat root to `sample/` subdirectory
- Tag `v0.5.1` for post-closeout commits