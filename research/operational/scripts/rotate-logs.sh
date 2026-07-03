#!/usr/bin/env bash
# Rotate service logs using our local logrotate config.
# Intended to run from cron every hour.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/../logs/.logrotate-state"

/usr/sbin/logrotate --state "$STATE_FILE" "$SCRIPT_DIR/logrotate.conf"
