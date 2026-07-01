---
title: OpenMarket Quickstart
emoji: "\U0001F4C8"
colorFrom: blue
colorTo: green
sdk: docker
app_port: 8888
pinned: false
license: apache-2.0
---

# OpenMarket Quickstart

This is a Hugging Face Space that demonstrates loading the
[OpenMarket BTC Polymarket](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)
sample split and computing a few summary statistics.

It runs a Jupyter notebook server (`jupyter lab --ip 0.0.0.0 --port 8888`) so
you can poke at the data directly from your browser.

## Files

- `Dockerfile` — minimal Python image with the notebook deps
- `requirements.txt` — runtime dependencies
- `notebooks/quickstart.ipynb` — sample loading + summary walkthrough

## Local dev

```bash
docker build -t openmarket-quickstart .
docker run -p 7860:7860 openmarket-quickstart
```

Then open http://localhost:7860.