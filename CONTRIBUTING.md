# Contributing

OpenMarket is structured as an infrastructure project. Contributions should make
the platform more reproducible, better documented, faster, or easier to extend.

## Good First Contribution Areas

- New technical indicators in the feature pipeline
- Dataset validation checks and schema tests
- Additional exchange collectors
- Benchmark harnesses for latency and throughput
- Research notebooks for order book imbalance, spread prediction, or labels
- Backtest realism improvements: fees, slippage, fill modeling, market impact
- Documentation improvements and diagrams

## Development

```bash
cargo fmt --all
cargo check --workspace
cargo clippy --workspace -- -D warnings
cargo test --workspace
python3 -m venv .venv && .venv/bin/pip install -r scripts/datasets/requirements.txt -r scripts/hf/requirements.txt
.venv/bin/python -m py_compile scripts/datasets/*.py scripts/hf/*.py
.venv/bin/python scripts/hf/validate_sample_split.py
```

Generated data, reports, and models should not be committed. Use Hugging Face
or GitHub Releases for artifacts.

## Hugging Face Auth (for dataset/model contributors)

The `scripts/hf/` helpers require a Hugging Face write token. For local
work:

```bash
brew install hf           # or: pip install -U 'huggingface_hub[cli]'
hf auth login
```

For CI / GitHub Actions, set `HF_TOKEN` as a repository secret with write
access to the `gregyoung14` org.

Contributors working on a **fork** should target their own dataset/model
repo by passing `--repo-id yourname/your-dataset` to `scripts/hf/*.py`.

## Dataset Schema Changes

The `sample/` split schema is the public contract. Any change to a Parquet
column name or type is a breaking change. Open an issue first describing:

- which columns are added, removed, or have their type changed
- the proposed `Dataset version` bump (`v0.1-sample` -> `v0.2-sample`, etc.)
- how the change is reflected in `datasets/hf/README.md` and
  `notebooks/quickstart.ipynb`

## Pull Request Checklist

- Explain the research or engineering motivation
- Include a reproducible command or test
- Document any schema, config, or dataset-version changes
- Avoid committing secrets, private keys, logs, or large generated files
- If HF behavior changed, run `scripts/hf/validate_sample_split.py` and
  re-execute `notebooks/quickstart.ipynb`
- If the GitHub release flow changed, run `cargo fmt` and `cargo clippy`
  locally
