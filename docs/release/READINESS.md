# Release readiness — what to check before flipping OpenMarket to public

This is the gate between "v0.1.0 private beta" and "v0.1.0 public GA".
Nothing here requires code; it's a checklist with commands.

## Source repo

- [ ] All commits pushed: `git log --oneline origin/main..main` is empty
- [ ] `cargo check --workspace` exits 0
- [ ] `cargo fmt --all -- --check` exits 0
- [ ] `cargo clippy --workspace -- -D warnings` exits 0
- [ ] `cargo test --workspace` exits 0
- [ ] `python3 -m py_compile scripts/datasets/*.py scripts/hf/*.py` exits 0
- [ ] `scripts/hf/validate_sample_split.py` exits 0 (PASS)
- [ ] `scripts/hf/benchmark_baseline.py` produces a fresh baseline under
      `benchmarks/baselines/`
- [ ] `notebooks/quickstart.ipynb` re-executes cleanly via `nbconvert --execute`
- [ ] `git grep -nE 'TODO|FIXME|XXX'` is empty (or every hit is intentional
      and documented in a tracked issue)
- [ ] No secrets in history: search the working tree and the latest diff for
      the patterns below. (Run on a fresh clone, not the local checkout that
      may have uncommitted state.)
      - Polygon private keys (`0x[a-fA-F0-9]{64}` as string literals)
      - L2 API credentials (`POLYMARKET_(API_KEY|SECRET|PASSPHRASE)`)
      - Bunny CDN access keys (`BUNNY_CDN_ACCESS_KEY`)
      - Operator wallet addresses (`0x13F7…2007` or any other 0x... we used)
      - VPS IP addresses, AWS/GCP/Azure account IDs

## Hugging Face

- [ ] `gregyoung14/openmarket-btc-polymarket` has the latest `sample/`
      split and `metadata/snapshot_manifest.{json,tsv}`
- [ ] `gregyoung14/openmarket-models` has the latest `models/hf/README.md`
      scaffold and the latest dataset-version reference
- [ ] HF dataset card `Dataset version:` matches the current dataset split
      (`scripts/hf/sync_version_with_tag.py --check --allow-mismatch`)
- [ ] `hf download … --dry-run` shows the expected file counts and sizes

## Docs and meta

- [ ] `LICENSE` is Apache-2.0 and committed
- [ ] `NOTICE` is committed and credits data sources
- [ ] `CODE_OF_CONDUCT.md` is committed
- [ ] `SECURITY.md` is committed and includes the disclosure policy
- [ ] `CONTRIBUTING.md` includes HF_TOKEN setup
- [ ] `.github/ISSUE_TEMPLATE/{bug,feature,dataset}.yml` are committed
- [ ] `.github/PULL_REQUEST_TEMPLATE.md` is committed
- [ ] `.github/dependabot.yml` is committed
- [ ] `.github/CODEOWNERS` is committed
- [ ] `README.md` references only files that exist in the tree
      (`scripts/release/`, `hf_space/` etc. were removed — see git log)

## Going public

```bash
# Flip visibility
gh repo edit gregyoung14/openmarket --visibility public --accept-visibility-change-consequences

# Trigger the GH Pages / docs deploy if applicable
gh workflow run release.yml
```

After flipping public:

- [ ] Watch for incoming issues over the next 24h, triage anything tagged
      `security` or `bug` immediately
- [ ] Verify the CI badge resolves from the public repo
- [ ] Verify the README badges (CI, Release, License, Dataset, Models) all
      render on the public repo's README
- [ ] Post the launch: see `docs/release/LAUNCH-POST.md`