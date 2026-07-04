# OpenMarket v0.5.2 — Public launch integrity tag

Public-launch release for the frozen OpenMarket research archive.

## Release integrity

- Source tag: `v0.5.2`
- GitHub repository: `github.com/gregyoung14/openmarket` (public)
- Dataset: `gregyoung14/openmarket-btc-polymarket`
  - `v0.4.3-unified` deduplicated unified split
  - `v0.2-full` complete 202-snapshot archive
  - `v0.1-sample` historical sample
- Models: `gregyoung14/openmarket-models`
  - `v0.2.1/` walk-forward logistic + Platt binary-outcome model
  - `v0.2/`, `v0.1/` historical model artifacts

This tag supersedes `v0.5.1` as the cited source tag because `v0.5.1` was cut
before the repository visibility and public-state documentation were fully
reconciled. Use `v0.5.2` in paper citations, Hugging Face cards, and release
metadata.

## Paper updates

- Shortened title and scoped novelty claim.
- Pooled OOS forecast comparison is now the primary benchmark; full-timeline
  rows are diagnostic only.
- Clock-offset validation separates drift from constant single-vantage offset
  and reports synchronization-free event-anchored response lags.
- Hugging Face row-count caveat explains default/indexed aggregate counts
  versus the deduplicated `unified/` split.
- Spread distribution is summarized as a compact table.

## Archival status

OpenMarket remains in archival shutdown. Active data collection, active strategy
development, and ongoing model maintenance have ended.
