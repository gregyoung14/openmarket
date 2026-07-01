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
python3 -m compileall -q research
```

Generated data, reports, and models should not be committed. Use Hugging Face or
GitHub Releases for artifacts.

## Pull Request Checklist

- Explain the research or engineering motivation
- Include a reproducible command or test
- Document any schema, config, or dataset-version changes
- Avoid committing secrets, private keys, logs, or large generated files
