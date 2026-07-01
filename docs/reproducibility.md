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

For the full research timeline, download the unified HF split:

```bash
.venv/bin/python datasets/download.py --split unified --out data/hf_cache
```

The backtester can consume Parquet directly once a SQLite bridge is wired;
until then, use a local SQLite export from the unified tables or the legacy
operator CDN path:

```bash
python3 datasets/download.py --legacy-cdn sample --out data/openmarket.db
cargo run -p v15_brier_calibration --release -- --db-path data/openmarket.db
```

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
