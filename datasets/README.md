# Datasets

Large datasets are released outside Git on Hugging Face:

- [`gregyoung14/openmarket-btc-polymarket`](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)

The HF repo holds two splits:

| Split | Size on disk | Contents |
|---|---:|---|
| `sample/` | ~371 KB | Smallest SQLite snapshot exported to Parquet. 12 tables, 9,352 rows. Used for tests, CI, and quickstarts. |
| `full/` | (planned, multi-GB) | All medium and large snapshots. Populated incrementally via `scripts/hf/release_split.py`. |

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

## Legacy Bunny CDN path

`datasets/download.py --snapshot <name-or-url>` is kept for migration from the
operator's Bunny CDN archive. It is **not** the recommended public path —
prefer the HF download above. The default `--snapshot sample` resolves to the
~10.9 GB first SQLite snapshot, so always pass `--out` to a path you intend.

## Do Not Commit

- SQLite databases
- Parquet partitions
- CSV exports
- HTML reports
- model binaries

All of these are produced under `data/hf_release/` and `data/raw/` locally;
both directories are gitignored.
