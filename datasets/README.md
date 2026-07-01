# Datasets

Large datasets are released outside Git on Hugging Face:

- [`gregyoung14/openmarket-btc-polymarket`](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)

The HF repo holds three splits:

| Split | Version | Contents |
|---|---|---|
| `unified/` | v0.3-unified | **Recommended.** Deduped timeline merged from all `full/` exports. |
| `full/` | v0.2-full | 10 per-snapshot exports (~456M rows before dedupe). Overlapping date ranges. |
| `sample/` | v0.1-sample | Smallest snapshot. 12 tables, 9,352 rows. CI and quickstarts. |

Per-snapshot metadata (`export_report.json`) and the master
`snapshot_manifest.{json,tsv}` (all 202 snapshots in the operator archive)
live under `metadata/` in the HF repo.

## Layout on the HF repo

```text
sample/
  binance_trades/date=YYYY-MM-DD/*.parquet
  binance_ticks_ms/date=YYYY-MM-DD/*.parquet
  polymarket_ticks_ms/date=YYYY-MM-DD/*.parquet
  lag_pairs_ms/date=YYYY-MM-DD/*.parquet
  binance_candles_{1s,5s,1m,5m,15m,1h}/date=YYYY-MM-DD/*.parquet
  market_meta/date=unpartitioned/*.parquet
  crossover_alerts/
  metadata/<snapshot>.export_report.json
full/
  ... same layout, multiple snapshots ...
unified/
  ... same layout, deduped single timeline ...
metadata/
  snapshot_manifest.json     # full inventory of operator archive
  snapshot_manifest.tsv      # same, TSV for easy diffing
README.md                    # HF dataset card
```

## Loading the sample split

The supported path is Hugging Face. Use `scripts/hf/validate_sample_split.py`
to round-trip it, `scripts/hf/benchmark_baseline.py` to measure load time, or
load directly with `huggingface_hub`:

```python
from pathlib import Path
from huggingface_hub import snapshot_download
import pyarrow.parquet as pq

root = Path(snapshot_download(
    "gregyoung14/openmarket-btc-polymarket",
    repo_type="dataset",
    allow_patterns=["sample/**", "metadata/**", "README.md"],
))
table = pq.read_table(next(root.rglob("binance_trades/*.parquet")))
print(table.num_rows, "rows; columns:", table.schema.names)
```

See `notebooks/quickstart.ipynb` for an end-to-end walkthrough.

## Downloading

```bash
.venv/bin/python datasets/download.py --split unified --out data/hf_cache
.venv/bin/python datasets/download.py --split sample --out data/hf_cache
```

## Legacy Bunny CDN path

`datasets/download.py --legacy-cdn <snapshot>` is kept for operator migration
from the Bunny CDN archive. It is **not** the recommended public path.
`--legacy-cdn sample` resolves to the ~10.9 GB first SQLite snapshot.

## Do Not Commit

- SQLite databases
- Parquet partitions
- CSV exports
- HTML reports
- model binaries

All of these are produced under `data/hf_release/` and `data/raw/` locally;
both directories are gitignored.
