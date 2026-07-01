# Stale Claims Audit — 2026-07-01

This audit lists public-facing statements that no longer match the actual
OpenMarket release state as of 2026-07-01.

## Ground Truth

- Hugging Face dataset repo contains:
  - `sample/` (`v0.1-sample`)
  - `full/` (`v0.2-full`) based on 10 published snapshots
  - `unified/` (`v0.3-unified`) derived from those 10 snapshots
- Hugging Face model repo contains a public `v0.1/` artifact set:
  - `binary_outcome_model.json`
  - `binary_outcome_metrics_*.json`
  - `model_manifest.json`
- Archive manifest inventory contains 202 CDN snapshots total.
- Local export reports currently cover 20 snapshots.
- Public queue metadata still classifies only the first 10 snapshots as
  published (`5 published-clean`, `5 published-partial`).

## Files With Stale or Misleading Release Language

### README and top-level docs

- `README.md`
  - Says pretrained model artifacts "will live in" Hugging Face Models.
    This is stale; a public `v0.1/` model payload now exists.
  - Says the first `v0.1.0` model is deferred to a future release.
    This is stale.
  - Does not clearly state that active data collection is over and the
    remaining task is publication of the existing archived CDN inventory.

### Model card

- `models/hf/README.md`
  - The "Planned Artifacts" heading is stale now that a `v0.1/` artifact set
    exists publicly.
  - The card should distinguish between published artifacts and still-missing
    provenance fields or limitations.

### Paper

- `paper/paper.md`
  - Abstract says `v0.2-full` is planned. Stale.
  - Contributions section says "sample live; full split planned". Stale.
  - ML section says the model repo is scaffolded and the first model artifact
    is deferred. Stale.
  - "Future Work" mixes genuine research extensions with archive-closeout tasks.
    That is misleading now that the project is being shut down.
  - "Open Source Release" section still describes the model repo as scaffolded.
    Stale.

### Release notes and release-adjacent docs

- `docs/release/RELEASE-NOTES-v0.1.0.md`
  - Correct as a historical document, but its "future releases" language should
    not be treated as the current project plan.
- `docs/release/RELEASE-NOTES-v0.2.0.md`
  - "Model version: deferred" is stale relative to the live HF model repo.
  - Follow-ups about rolling forward to later releases are stale as the project
    is shutting down.
- `docs/release/RELEASE-NOTES-v0.3.0.md`
  - "Model version: deferred" is stale.
  - Follow-ups describe an ongoing roadmap rather than a finite archive-closeout.
- `docs/release/LAUNCH-POST.md`
  - Refers to `full/` as planned and models as follow-up. Stale draft.
- `docs/release/github-release-checklist.md`
  - Historical checklist still says models were deferred.

## Not Stale But Easy To Misread

- `docs/release/RELEASE-NOTES-v0.4.0.md`
  - This reads like a completed release but is currently only a local draft.
  - It should not be treated as published truth until the associated dataset and
    provenance artifacts are verified and tagged.

## Archival Gap That Still Remains

These are not stale statements; they are the actual remaining closure tasks:

- Publish the remaining CDN archive snapshots beyond the 10 already live in
  `full/`.
- Reconcile the local 20 exported snapshots with HF publication state.
- Update queue metadata so published coverage matches reality.
- Decide whether to publish additional feature splits beyond the current
  datasets, or freeze the archive at `sample/`, `full/`, `unified/`, and the
  current model artifacts.
