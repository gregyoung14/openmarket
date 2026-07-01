# OpenMarket release pipeline

This document is the operational runbook for cutting a new release of the
OpenMarket research platform. It covers both the GitHub source release and the
Hugging Face dataset/model releases, and explains how the helper scripts in
`scripts/` fit together.

## Pieces in the pipeline

```text
  scripts/datasets/export_many_snapshots.py
    └─> per-snapshot scripts/datasets/export_snapshot_v2.py
        └─> writes data/hf_release/full_parquet/<table>/date=YYYY-MM-DD/*.parquet
            + metadata/<snapshot>.export_report.json

  scripts/datasets/merge_partitions.py
    └─> dedupes full_parquet/ -> unified_parquet/
        + metadata/merge_quality_report.json

  scripts/hf/validate_sample_split.py       (round-trip + row-count check)
  scripts/hf/benchmark_baseline.py          (timing + row-count metrics)
  scripts/hf/upload_split.py                (HF upload with multi-commit)
  scripts/hf/bump_dataset_version.py        (dataset card version bump)
  scripts/hf/release_split.py               (orchestrator for the above)
```

For GitHub:

```text
  .github/workflows/release.yml             (auto-tag HF on `v*` push)
  .github/workflows/ci.yml                  (cargo + python validation)
  scripts/hf/                               (HF release automation)
```

## Cutting a GitHub release

1. Make sure `main` is green (CI badge).
2. Update version in `Cargo.toml` workspace root.
3. Write `docs/release/RELEASE-NOTES-vX.Y.Z.md`.
4. Commit, push, then:
   ```bash
   git tag -a vX.Y.Z -m "OpenMarket vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. The `release.yml` workflow auto-creates the GitHub release and tags the HF
   dataset/model repos with the same version.

## Cutting a dataset release

Small change (sample):

```bash
.venv/bin/python scripts/hf/release_split.py \
    --split sample \
    --new-version v0.2-sample
```

Large change (full split):

```bash
.venv/bin/python scripts/hf/release_split.py \
    --split full \
    --queue clean \
    --batch-size 10 \
    --batch-index 1 \
    --min-bytes 0 \
    --reports-dir <OPENMARKET_REPO>/data/hf_release/full_parquet/metadata \
    --new-version v0.2-full
```

For the clean-snapshot publishing lane, keep queue state in
`docs/release/full-snapshot-publish-status.json`, use
`docs/release/full-snapshot-batching.md` as the runbook, and keep
`docs/release/snapshot_recovery_status.json` as the recovery ledger. The
`>= 10 MB` backlog is already exhausted as of 2026-07-01, so additional clean
batches must set `--min-bytes 0`.

Unified deduped timeline (recommended public release):

```bash
.venv/bin/python scripts/hf/release_split.py \
    --split unified \
    --skip-export \
    --new-version v0.3-unified
```

Each step is also individually runnable:

```bash
# 1. List the next clean batch without exporting it yet
.venv/bin/python scripts/datasets/export_many_snapshots.py \
    --manifest <OPENMARKET_REPO>/data/hf_release/metadata/snapshot_manifest.json \
    --reports-dir <OPENMARKET_REPO>/data/hf_release/full_parquet/metadata \
    --status-file docs/release/full-snapshot-publish-status.json \
    --queue clean \
    --min-bytes 0 \
    --batch-size 10 \
    --batch-index 1 \
    --list-only \
    --write-plan data/hf_release/metadata/clean-batch-01.plan.json

# 2. Export that exact batch from the Bunny archive
.venv/bin/python scripts/datasets/export_many_snapshots.py \
    --manifest <OPENMARKET_REPO>/data/hf_release/metadata/snapshot_manifest.json \
    --reports-dir <OPENMARKET_REPO>/data/hf_release/full_parquet/metadata \
    --status-file docs/release/full-snapshot-publish-status.json \
    --snapshot-ids-file data/hf_release/metadata/clean-batch-01.plan.json \
    --queue clean \
    --min-bytes 0

# 3. Round-trip the produced split
.venv/bin/python scripts/hf/validate_sample_split.py --sample-dir full

# 4. Upload to HF
.venv/bin/python scripts/hf/upload_split.py --split full

# 5. Bump dataset card version
.venv/bin/python scripts/hf/bump_dataset_version.py --set v0.2-full

# 6. Commit + push the card change
git add datasets/hf/README.md
git commit -m "Bump dataset version to v0.2-full"
git push origin main
```

## Validation

Before tagging anything, run:

```bash
cargo check --workspace                       # 0 errors expected
cargo fmt --all -- --check                    # style
cargo clippy --workspace -- -D warnings       # lints
python -m py_compile scripts/datasets/*.py scripts/hf/*.py
.venv/bin/python scripts/hf/validate_sample_split.py
```

CI runs the same checks on every push and PR.

## Skipping creds and secrets

The HF upload requires `HF_TOKEN` in the environment. The CI release workflow
reads it from `secrets.HF_TOKEN`. Locally, `hf auth login` once stores a
cached token in `~/.cache/huggingface/`.

No production secrets (Bunny CDN, AWS, wallets, etc.) live in this repo or
the HF datasets. The Bunny download uses a public CDN URL; per-snapshot
checksums in `metadata/snapshot_manifest.json` let consumers verify
integrity without out-of-band credentials.
