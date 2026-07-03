#!/usr/bin/env bash

set -euo pipefail

SNAPSHOT_URL="${1:-https://YOUR_STORAGE_ZONE.b-cdn.net/polymarket-bot/polymarket_btc_data_2026-03-14_193215.db.gz}"
OUTPUT_DB="${2:-./polymarket_btc_data.db}"
OUTPUT_DIR="$(dirname "$OUTPUT_DB")"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
OUTPUT_DB="$OUTPUT_DIR/$(basename "$OUTPUT_DB")"

echo "Downloading and decompressing snapshot to $OUTPUT_DB"
curl -fL "$SNAPSHOT_URL" | gunzip -c > "$OUTPUT_DB"

echo "Running integrity check"
INTEGRITY_OUTPUT="$(sqlite3 "$OUTPUT_DB" "PRAGMA integrity_check;" 2>&1 || true)"
if [[ "$INTEGRITY_OUTPUT" != "ok" ]]; then
	echo "$INTEGRITY_OUTPUT" >&2
	rm -f "$OUTPUT_DB"
	echo "Snapshot integrity check failed" >&2
	exit 1
fi

echo
echo "Snapshot ready"
echo "  DATABASE_FILE=$OUTPUT_DB cargo run --release"
