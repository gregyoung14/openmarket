# Reproducibility

The project should be reproducible from four identifiers:

- source commit
- dataset version
- model version
- config file

## Local Reproduction

```bash
git clone https://github.com/gregyoung14/openmarket.git
cd openmarket
python3 datasets/download.py --snapshot sample --out data/openmarket.db
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
