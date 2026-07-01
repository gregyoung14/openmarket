# Reproducibility

The project should be reproducible from four identifiers:

- source commit
- dataset version
- model version
- config file

## Local Reproduction

The fastest path uses the small Hugging Face sample split (≈204 KB, downloads in seconds):

```bash
git clone https://github.com/gregyoung14/openmarket.git
cd openmarket
python3 -m venv .venv && .venv/bin/pip install pyarrow huggingface_hub
.venv/bin/python scripts/hf/validate_sample_split.py    # round-trip + row-count check
cargo run -p v15_brier_calibration --release -- --db-path <path-to-sqlite-from-full-split>
```

For a full reproduction using the multi-gigabyte SQLite snapshots (operator
archive), use the legacy Bunny CDN path:

```bash
python3 datasets/download.py --snapshot sample --out data/openmarket.db
cargo run -p v15_brier_calibration --release -- --db-path data/openmarket.db
```

Note: `datasets/download.py --snapshot sample` resolves to the ~10.9 GB
first SQLite snapshot in the operator archive, not the HF `sample/` split.
For the HF split use the `snapshot_download` API shown above.

## Docker Reproduction

```bash
cp configs/openmarket.example.env .env
docker compose -f docker/docker-compose.yml up
```

## Required Reporting

Each benchmark or paper result should report:

- CPU model
- RAM
- storage type
- OS
- Rust version
- Python version
- dataset version
- model version
- command
- random seed
