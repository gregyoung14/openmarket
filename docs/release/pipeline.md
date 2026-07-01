# OpenMarket release pipeline

This document is the operational runbook for cutting a new release of the
OpenMarket research platform. It covers both the GitHub source release and the
Hugging Face dataset/model releases, and explains how the helper scripts in
`scripts/` fit together.

## Pieces in the pipeline

```text
  scripts/datasets/export_many_snapshots.py
    └─> per-snapshot scripts/datasets/export_snapshot_to_parquet.py
        └─> writes data/hf_release/<split>_parquet/<table>/date=YYYY-MM-DD/*.parquet
            + metadata/<snapshot>.export_report.json

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
    --max-snapshots 5 \
    --min-bytes 10000000 \
    --new-version v0.2-full
```

Each step is also individually runnable:

```bash
# 1. Export N snapshots from the Bunny archive
.venv/bin/python scripts/datasets/export_many_snapshots.py \
    --max-snapshots 5

# 2. Round-trip the produced split
.venv/bin/python scripts/hf/validate_sample_split.py --sample-dir full

# 3. Upload to HF
.venv/bin/python scripts/hf/upload_split.py --split full

# 4. Bump dataset card version
.venv/bin/python scripts/hf/bump_dataset_version.py --set v0.2-full

# 5. Commit + push the card change
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