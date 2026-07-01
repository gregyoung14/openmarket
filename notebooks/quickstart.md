# OpenMarket Quickstart notebook

This notebook loads the published sample split from Hugging Face and walks
through basic inspection, joining Binance trades with Polymarket ticks, and
computing a 1-minute mid-price series.

## Run it

```bash
.venv/bin/jupyter nbconvert --to notebook --execute notebooks/quickstart.ipynb \
    --output quickstart.executed.ipynb
```

Or open it interactively:

```bash
.venv/bin/jupyter lab notebooks/quickstart.ipynb
```

The notebook expects to be run from the repo root so that it can resolve
`scripts/hf/*.py` via `%run` or import.