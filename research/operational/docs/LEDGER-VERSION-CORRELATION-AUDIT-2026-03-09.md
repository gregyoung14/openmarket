# Ledger Version Correlation Audit (2026-03-09)

## Scope

Goal: correlate git/version history for signal-engine with what was actually stamped in `data/trade_ledger.json` **after** the safety cutoff.

Safety cutoff used:
- Commit `53acef4` (`fix: properly parse market_side and side in signal/execution pipeline`)
- Commit time: `2026-03-01 16:13:58 -0500` = `2026-03-01T21:13:58+00:00`

Audit inputs:
- Git history (`rust-services/signal-engine`, `rust-services/btc-common/src/version.rs`, `services/redeem-positions/redeem_positions_service.py`)
- Runtime entry logs (`logs/execution-engine.log`)
- Ledger (`data/trade_ledger.json`)

## Signal Version Timeline (Source of Truth in Git)

From `rust-services/btc-common/src/version.rs` history:

- `f9a2312` at `2026-03-01 17:48:42 +0000`: `SIGNAL_VERSION=v11`
- `27cb5ef` at `2026-03-01 13:56:56 -0500`: `SIGNAL_VERSION=v8-fix-validation`
- `6973621` at `2026-03-05 01:10:10 +0000`: `SIGNAL_VERSION=v8-fix`
- `cf18720` at `2026-03-06 15:41:11 -0500`: `SIGNAL_VERSION=v14`

Related execution commit:
- `5f6311d` at `2026-03-07 05:48:19 +0000`: execution-engine v15 parser/sizing changes

## Ledger Tag Transition Timeline (Post-Cutoff)

From `data/trade_ledger.json` (`redeemed_at` chronological):

- `2026-03-01T22:33:03Z`: `v9.4.1-regime`
- `2026-03-02T03:50:19Z`: `v8-fix`
- `2026-03-03T06:04:18Z`: `v8.1-fix`
- `2026-03-03T23:04:06Z`: `v8.2-fix`
- `2026-03-06T22:30:42Z`: `v15`

Counts after cutoff:
- `v8-fix`: 77
- `v8.2-fix`: 64
- `v8.1-fix`: 36
- `v15`: 15
- `v9.4.1-regime`: 3

## Runtime vs Ledger Correlation (By Market Slug)

Method:
- Parse `logs/execution-engine.log` ENTRY lines (`market` + `version`)
- Join to ledger rows by `slug`
- Compare runtime signal `version` vs ledger `signal_version`

Result:
- Post-cutoff entries checked: 195
- Exact version matches: 97
- Mismatches: 98 (50.3%)

Confusion matrix (`runtime_version -> ledger_version`):

- `v8-fix-validation -> v8-fix`: 77
- `v8.2-fix -> v8.2-fix`: 62
- `v8.1-fix -> v8.1-fix`: 35
- `v14 -> v15`: 15
- `v8-fix-validation -> v9.4.1-regime`: 3
- `v8-fix -> v8.2-fix`: 2
- `v8-fix-validation -> v8.1-fix`: 1

## Findings

1. Ledger naming drift is real and large.
- About half of post-cutoff rows do not match runtime signal version.

2. `v8-fix` in ledger mostly maps to runtime `v8-fix-validation`.
- This looks like normalization/override, not true signal runtime identity.

3. `v15` in ledger currently maps to runtime `v14` signals.
- This is expected under current policy because ledger tag now follows execution version (`execution_version=v15`), not signal version.

4. `v8.1-fix` / `v8.2-fix` appear in ledger but are not represented in `btc-common` SIGNAL_VERSION history.
- These names likely came from other runtime/config paths and are not centrally governed.

## Repro Command

```bash
python3 scripts/audit_signal_vs_ledger_versions.py
```

## Normalization Applied (2026-03-09)

Action taken:
- Stopped `redeem-positions.service` to prevent in-memory ledger overwrite.
- Applied runtime-truth normalization with:

```bash
python3 scripts/backfill_signal_versions_from_runtime_post_cutoff.py --apply
```

- Backup created: `data/trade_ledger.json.bak-signal-runtime-normalize-20260309-221937`
- Restarted `redeem-positions.service`.

Post-apply verification:
- `post_cutoff_entries=195`
- `exact_matches=195`
- `mismatches=0`

Post-apply matrix:
- `v8-fix-validation -> v8-fix-validation`: 81
- `v8.2-fix -> v8.2-fix`: 62
- `v8.1-fix -> v8.1-fix`: 35
- `v14 -> v14`: 15
- `v8-fix -> v8-fix`: 2
