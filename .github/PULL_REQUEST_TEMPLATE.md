## Summary

<!-- One-paragraph description of the change. -->

## Linked issues

<!-- Use `Fixes #NNN` to auto-close. -->

## Testing

<!--
- [ ] `cargo check --workspace` passes
- [ ] `cargo fmt --all -- --check` passes
- [ ] `cargo clippy --workspace -- -D warnings` passes
- [ ] `python -m py_compile scripts/datasets/*.py scripts/hf/*.py` passes
- [ ] `scripts/hf/validate_sample_split.py` passes (if HF behavior changed)
- [ ] `notebooks/quickstart.ipynb` re-executes (if sample schema changed)
-->

## Release impact

<!--
- [ ] No release needed
- [ ] Patch release (source only)
- [ ] New dataset split on HF (run `scripts/hf/release_split.py`)
- [ ] HF dataset version bump (run `scripts/hf/bump_dataset_version.py`)
- [ ] GitHub tag required
-->

## Checklist

- [ ] I have read [CONTRIBUTING.md](./CONTRIBUTING.md)
- [ ] I have added/updated tests where applicable
- [ ] I have updated docs that would be misleading otherwise