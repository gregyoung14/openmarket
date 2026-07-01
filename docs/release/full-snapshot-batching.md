# Full Snapshot Clean-Batch Track

This track exists to keep Hugging Face publication moving for snapshots that
are not already published and are not on a known-corrupt hold lane.

## Current queue state

Source of truth on 2026-07-01:

- Archive manifest: `<OPENMARKET_REPO>/data/hf_release/metadata/snapshot_manifest.json`
- Published export reports: `<OPENMARKET_REPO>/data/hf_release/full_parquet/metadata`
- Tracked queue file: `docs/release/full-snapshot-publish-status.json`

As of 2026-07-01:

- Total archive snapshots: `202`
- Already published (queue metadata): `202` (`202 published-clean`, `0 published-partial`)
- Local export reports on disk: `202`
- Remaining unbatched snapshots: `0` (clean backlog complete as of 2026-07-01)
- Remaining snapshots `>= 10 MB`: `0`
- Clean backlog: **complete** (batches 01–20, July 2026)

## Queue policy

- `published-clean`: already on HF and should not be selected again.
- `published-partial`: already on HF but had partial table exports; keep them
  out of the clean lane.
- `corrupt`: explicit hold queue for snapshots that fail clean-lane export.
  Add a snapshot here as soon as it proves bad so the next clean batch can run
  immediately.
- `clean`: derived queue, meaning "present in the archive manifest, not already
  published, and not listed under `corrupt`".

## Completed batches

- `clean-batch-01`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-02`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-03`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-04`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-05`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-06`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-07`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-08`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-09`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-10`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-11`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-12`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-13`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-14`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-15`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-16`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-17`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-18`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-19`: exported and queued as `published-clean` (10 snapshots)
- `clean-batch-20`: exported and queued as `published-clean` (2 snapshots)

## Clean backlog status

The clean publication lane is complete. All 202 CDN manifest snapshots are
exported and reflected in `docs/release/full-snapshot-publish-status.json`.
No further `clean` batches are pending.

## Operator flow

1. List the next clean batch and write a plan file:

```bash
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
```

2. Export exactly that plan:

```bash
.venv/bin/python scripts/datasets/export_many_snapshots.py \
  --manifest <OPENMARKET_REPO>/data/hf_release/metadata/snapshot_manifest.json \
  --reports-dir <OPENMARKET_REPO>/data/hf_release/full_parquet/metadata \
  --status-file docs/release/full-snapshot-publish-status.json \
  --snapshot-ids-file data/hf_release/metadata/clean-batch-01.plan.json \
  --queue clean \
  --min-bytes 0 \
  --out-dir data/hf_release/full_parquet
```

3. If one snapshot fails because it is corrupt, move its id into the
   `corrupt` queue in `docs/release/full-snapshot-publish-status.json`, then
   rerun step 2 with a refreshed plan for the same batch index.

4. After the clean batch validates and uploads, move the successful snapshot
   ids from implicit `clean` into `published-clean` or `published-partial`
   according to their export reports.

## Why this is separate from the old full release flow

The original full release path assumed a single size-filtered queue and local
metadata colocated with the current output directory. That breaks once:

- the published metadata lives outside the clean-track worktree,
- the `>= 10 MB` backlog is already exhausted, and
- corrupt snapshots must not stall the rest of the archive.

The updated scripts now support explicit queueing, external published-report
metadata, exact-plan replay, and batch indexing so clean snapshots can keep
moving independently of the corrupt hold lane.
