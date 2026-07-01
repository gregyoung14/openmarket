# OpenMarket — Show HN / launch draft

> Draft for Hacker News "Show HN" submission and a longer-form blog post.
> Tighten the title and trim the first paragraph before posting.

## Show HN

**Title:** Show HN: OpenMarket – high-frequency Binance/Polymarket data + Rust backtester (Apache 2.0)

**Body:**

I built and open-sourced a research platform for studying Polymarket's
15-minute BTC binary markets against Binance BTC/USDT spot data. The goal
is reproducible prediction-market research — code, schemas, datasets,
notebooks, and a systems paper all under Apache 2.0.

What's in the release:

- Rust workspace (10 crates) for Binance + Polymarket WebSocket collectors,
  multi-market recorder, lag-pair export, signal engine, paper executor,
  backtester, and shared types
- Hugging Face dataset with a small (~204 KB) `sample/` split for CI and a
  planned `full/` split covering ~45 GB of multi-snapshot Parquet
- Quickstart notebook that loads the sample, walks table schemas, and
  joins Binance trades with Polymarket ticks
- A reproducible release pipeline (`scripts/hf/release_split.py`) and
  GitHub Actions CI that round-trips the published sample split

The dataset schema, the systems paper, and the data-collection
methodology are documented. The trading strategy code in `research/`
covers drift, order-flow imbalance, Brier-score calibration, and several
legacy ML baselines (XGBoost, LightGBM, stacked classifiers).

This is research infrastructure, not a black-box bot. The full historical
archive is multi-gigabyte and released incrementally through Hugging
Face. The sample split is enough to run the pipeline end-to-end without
downloading the full archive.

- Code: https://github.com/gregyoung14/openmarket (private during v0.1.0
  beta, public at GA)
- Dataset: https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket
- Models: https://huggingface.co/gregyoung14/openmarket-models (scaffold only)
- Paper: `paper/paper.md` in the repo

Happy to discuss Polymarket microstructure, Rust async collector design,
HF dataset layouts, or whatever else is useful.

---

## Long-form blog post outline

Target length: ~1500 words.

### 1. Why this exists

Prediction markets have been a research interest for a long time, but
they've mostly been studied at the resolution of weekly or monthly
contracts. Polymarket's 15-minute BTC up/down markets change that — a new
contract every quarter hour, settled on-chain against a public reference
price. That makes them an unusually clean substrate for studying
short-horizon price formation, order-book microstructure, and
information flow between venues.

The existing research infrastructure for this kind of question is
mostly built around daily klines and monthly options. I wanted a
research platform where the primary unit of analysis is the
millisecond-resolution tick and the on-chain settlement event.

### 2. What's in the box

- Rust collectors for Binance trades and Polymarket CLOB events
- A multi-market SQLite recorder with WAL mode and date-partitioned
  exports
- Lag-pair export that joins Binance and Polymarket events at
  millisecond resolution (with a documented definition of "lead" and
  "lag")
- A signal engine that combines drift estimation with calibration
- A reproducible backtester and a paper-trade executor
- Hugging Face dataset release with a tiny sample for CI and a multi-GB
  full split for real research
- A systems paper draft covering sync, feature engineering, calibration,
  and limitations

### 3. What's deliberately not in the box

- A live trading strategy that you can point at your own money. The
  execution engine is documented and the SDK calls are real, but I am
  not shipping a "run this and you will make money" configuration.
- Pretrained models. The model card scaffolding exists on Hugging Face
  but no v0.1 weights ship with the release. Training and evaluating
  pretrained models on the full dataset is a follow-up.
- The full historical archive as a single download. The archive is
  ~46 GB compressed across 202 snapshots. We expose the inventory and
  ship a sample; the rest comes through Hugging Face splits.

### 4. How to use it

```bash
git clone https://github.com/gregyoung14/openmarket
cd openmarket
cargo check --workspace
python3 -m venv .venv
.venv/bin/pip install -r scripts/datasets/requirements.txt \
                     -r scripts/hf/requirements.txt
.venv/bin/python scripts/hf/validate_sample_split.py
.venv/bin/jupyter nbconvert --to notebook --execute notebooks/quickstart.ipynb
```

That's enough to load the published sample split, walk table schemas,
and compute a 1-minute Polymarket mid-price series.

### 5. What I'd love feedback on

- Schema: are the parquet partitions in `sample/` useful as-is, or do
  researchers want a flat layout with a manifest?
- Release cadence: should `full/` publish one snapshot per release, or
  batch several?
- HF dataset card: missing fields? Better way to surface "known
  limitations"?
- Backtester API: is the existing CLI ergonomic for researchers who
  want to plug in their own signal?

### 6. Limits and disclosures

The research is on Polymarket's public CLOB and Binance's public
trade stream; no private data or non-public APIs are involved. The
maintained repo has no production credentials in it — earlier private
development snapshots did, and those credentials were rotated before
the public release. The systems paper has a "Limitations" section that
documents collector-host clock drift, WebSocket reconnect gaps, and
top-of-book backtest fill assumptions.

This is not investment advice. The trading strategies shipped as
research artifacts are not a recommendation that anyone trade on the
basis of the code or the results here.