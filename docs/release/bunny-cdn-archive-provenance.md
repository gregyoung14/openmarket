# Bunny CDN / Storage Connection Info

> **Operational provenance.** These notes were copied from the original
> collection infrastructure repo to preserve archive lineage. Host-specific
> paths and systemd unit names remain for internal reference; access keys are
> not stored here and must be read from the live service configuration.

Last verified: 2026-07-01

This document collects the connection details needed to find, download, and inventory the Polymarket BTC SQLite snapshot archive.

## Canonical Live Database

- Live SQLite database path: `<DATA_VOLUME>/polymarket_btc_data.db`
- Backup service: `db-backup`
- Backup service port: `8007`
- Backup state file: `<DATA_VOLUME>/polymarket_btc_data.backup_state.json`

Health check:

```bash
curl -sS http://127.0.0.1:8007/health | jq .
```

Current live config observed from `/health`:

- backup interval: `21600` seconds, or 6 hours
- prune interval: `21600` seconds, or 6 hours
- prune retention: `0` days
- recent-backup prune requirement: `43200` seconds, or 12 hours

## Bunny Public CDN

Use this for direct public downloads when you already know the snapshot filename.

- CDN base URL: `https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot`
- Filename pattern: `polymarket_btc_data_<YYYY-MM-DD>_<HHMMSS>.db.gz`

Known public snapshot:

```text
https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz
```

Download and decompress:

```bash
curl -fL "https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz" \
  | gunzip -c > polymarket_btc_data.db
```

Verify:

```bash
sqlite3 polymarket_btc_data.db "PRAGMA integrity_check;"
sqlite3 polymarket_btc_data.db "SELECT count(*) FROM binance_trades;"
```

Public directory listing is disabled, so use Bunny Storage API for inventory.

## Bunny Storage API

Use this for authenticated listing, inventory, and archive processing.

- Storage region: `ny`
- Storage API base: `https://ny.storage.bunnycdn.com`
- Storage zone: `YOUR_STORAGE_ZONE`
- Storage folder: `polymarket-bot`
- Folder list endpoint: `https://ny.storage.bunnycdn.com/YOUR_STORAGE_ZONE/polymarket-bot/`
- Auth header: `AccessKey: <BUNNY_CDN_ACCESS_KEY>`

The access key is configured in the user systemd unit, not duplicated here.

Unit location:

```text
<SERVICE_USER_HOME>/.config/systemd/user/db-backup.service
```

Show loaded service config:

```bash
systemctl --user cat db-backup.service
```

Extract the key into an environment variable for one shell:

```bash
export BUNNY_CDN_ACCESS_KEY="$(
  systemctl --user cat db-backup.service |
    sed -n 's/^Environment=BUNNY_CDN_ACCESS_KEY=//p' |
    head -1
)"
```

List snapshots:

```bash
curl -fsS \
  -H "AccessKey: $BUNNY_CDN_ACCESS_KEY" \
  "https://ny.storage.bunnycdn.com/YOUR_STORAGE_ZONE/polymarket-bot/" |
  jq -r '.[] | [.ObjectName, .Length, .LastChanged] | @tsv'
```

Expected result shape:

```text
polymarket_btc_data_2026-03-14_193215.db.gz    10935294993    2026-03-14T19:32:16.008
polymarket_btc_data_2026-03-22_215354.db.gz     9691222331    2026-03-22T21:53:55.114
...
```

On 2026-07-01 this listing returned HTTP `200` with `202` objects.

## Backup Service Unit

Repo copy:

```text
systemd/user/db-backup.service
```

Installed user unit:

```text
<SERVICE_USER_HOME>/.config/systemd/user/db-backup.service
```

Important environment values:

```text
DB_BACKUP_INTERVAL_SECS=21600
DB_PRUNE_INTERVAL_SECS=21600
DB_PRUNE_RETENTION_DAYS=0
DB_PRUNE_REQUIRED_RECENT_BACKUP_MAX_AGE_SECS=43200
DB_VACUUM_TEMP_DIRS=<DATA_VOLUME>/db-vacuum-staging:<SERVICE_USER_HOME>/db-vacuum-staging:/var/tmp/polymarket-db-maintenance:/tmp
```

Service controls:

```bash
systemctl --user status db-backup.service
systemctl --user restart db-backup.service
journalctl --user -u db-backup.service -f
```

If systemd reports that the unit changed on disk:

```bash
systemctl --user daemon-reload
systemctl --user restart db-backup.service
```

## Service HTTP Endpoints

Health:

```bash
curl -sS http://127.0.0.1:8007/health | jq .
```

Manual backup:

```bash
curl -X POST http://127.0.0.1:8007/backup
```

Manual prune:

```bash
curl -X POST http://127.0.0.1:8007/prune
```

## Archive Processor Inputs

For a Hugging Face/public release archive builder, the minimum inputs are:

- Bunny Storage list endpoint: `https://ny.storage.bunnycdn.com/YOUR_STORAGE_ZONE/polymarket-bot/`
- Bunny access key from `db-backup.service`
- CDN download base: `https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot`
- Filename regex: `^polymarket_btc_data_(\d{4}-\d{2}-\d{2})_(\d{6})\.db\.gz$`
- Output format target: typed Parquet partitions by UTC date

Recommended processing shape:

1. List snapshots from Bunny Storage API.
2. Sort by timestamp parsed from filename.
3. Download one `.db.gz` at a time from the public CDN or storage API.
4. Stream-decompress to a temporary SQLite file.
5. Run `PRAGMA integrity_check;`.
6. Export typed Parquet partitioned by UTC date.
7. Deduplicate overlap between snapshots by stable keys and timestamps.
8. Validate row counts, time ranges, null rates, and schema.
9. Upload to Hugging Face with a dataset card and sharded/large-folder upload.

## Related Files

- `docs/TDR-backtest-database-access.md`
- `rust-services/db-backup/src/main.rs`
- `systemd/user/db-backup.service`
- `scripts/ml/bootstrap_archived_snapshot.sh`
- `<DATA_VOLUME>/code/binance-historical-backtest/TDR-CDN.md`
