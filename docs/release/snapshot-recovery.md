# Snapshot Recovery Lanes

This worktree isolates known bad snapshots into a side lane so normal `full/`
publication runs do not pick them up by accident. It is now the base branch
for the operator-ready snapshot flow.

## Source of truth

- Inventory: `/Users/greg/Software/openmarket/data/hf_release/metadata/snapshot_manifest.json`
- Recovery ledger: `docs/release/snapshot_recovery_status.json`
- Publish queue state: `docs/release/full-snapshot-publish-status.json`

Use the publish queue file as the control plane for clean publication. Use the
recovery ledger to explain why a snapshot belongs in the side lane and what the
operator should try next.

The JSON policy tracks three recovery categories:

1. `table_level_page_corruption`
2. `duckdb_unopenable_sqlite`
3. `post_prune_residue`

Seeded quarantine set from the current release notes:

- `polymarket_btc_data_2026-03-29_215354`
- `polymarket_btc_data_2026-03-22_215354`
- `polymarket_btc_data_2026-05-13_183517`
- `polymarket_btc_data_2026-04-21_211838`

## Queues and lanes

- `clean`: operator-ready publish queue. Derived from the manifest minus
  already published snapshots and minus anything explicitly listed under
  `corrupt` in `full-snapshot-publish-status.json`.
- `corrupt`: explicit hold queue for snapshots that fail clean export.
- `published-clean` / `published-partial`: snapshots already released on HF.
- `publish` / `recovery` / `all`: deprecated lane aliases kept only for
  backwards compatibility with old commands.

## Commands

Plan the next clean batch:

```bash
.venv/bin/python scripts/datasets/export_many_snapshots.py \
    --manifest /Users/greg/Software/openmarket/data/hf_release/metadata/snapshot_manifest.json \
    --reports-dir /Users/greg/Software/openmarket/data/hf_release/full_parquet/metadata \
    --status-file docs/release/full-snapshot-publish-status.json \
    --queue clean \
    --min-bytes 0 \
    --batch-size 10 \
    --batch-index 1 \
    --list-only \
    --write-plan data/hf_release/metadata/clean-batch-01.plan.json
```

Export the planned clean batch:

```bash
.venv/bin/python scripts/hf/release_split.py \
    --split full \
    --queue clean \
    --batch-size 10 \
    --batch-index 1 \
    --min-bytes 0 \
    --reports-dir /Users/greg/Software/openmarket/data/hf_release/full_parquet/metadata \
    --new-version v0.2-full
```

List the explicit corrupt hold queue:

```bash
.venv/bin/python scripts/datasets/export_many_snapshots.py \
    --manifest /Users/greg/Software/openmarket/data/hf_release/metadata/snapshot_manifest.json \
    --status-file docs/release/full-snapshot-publish-status.json \
    --queue corrupt \
    --list-only
```

If a clean-lane export proves corrupt, move its snapshot id into the `corrupt`
queue in `full-snapshot-publish-status.json` immediately, then rerun the same
clean batch without waiting on recovery. Use `snapshot_recovery_status.json`
to record category, reason, and operator action.
