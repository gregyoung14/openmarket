#!/usr/bin/env bash

set -uo pipefail

GIB=1073741824

VOLUME_PATH="${MONITOR_VOLUME_PATH:-/var/lib/polymarket}"
DB_FILE="${MONITOR_DB_FILE:-/var/lib/polymarket/polymarket_btc_data.db}"
BINANCE_HEALTH_URL="${MONITOR_BINANCE_HEALTH_URL:-http://127.0.0.1:8001/health}"

WARN_DISK_PCT="${MONITOR_WARN_DISK_PCT:-80}"
CRIT_DISK_PCT="${MONITOR_CRIT_DISK_PCT:-90}"
WARN_DB_GIB="${MONITOR_WARN_DB_GIB:-60}"
CRIT_DB_GIB="${MONITOR_CRIT_DB_GIB:-80}"
WARN_FREE_GIB="${MONITOR_WARN_FREE_GIB:-25}"
CRIT_FREE_GIB="${MONITOR_CRIT_FREE_GIB:-10}"

WARN_DB_BYTES=$((WARN_DB_GIB * GIB))
CRIT_DB_BYTES=$((CRIT_DB_GIB * GIB))
WARN_FREE_BYTES=$((WARN_FREE_GIB * GIB))
CRIT_FREE_BYTES=$((CRIT_FREE_GIB * GIB))

status="OK"
exit_code=0
messages=()

set_status() {
    local next_status="$1"
    local next_code="$2"
    local message="$3"

    messages+=("${next_status}: ${message}")
    if (( next_code > exit_code )); then
        status="$next_status"
        exit_code="$next_code"
    fi
}

format_gib() {
    awk -v bytes="$1" 'BEGIN { printf "%.2f", bytes / 1073741824 }'
}

if df_line="$(df -Pk "$VOLUME_PATH" 2>/dev/null | awk 'NR == 2 { print $2, $3, $4, $5 }')" && [[ -n "$df_line" ]]; then
    read -r total_kb used_kb avail_kb used_pct_raw <<< "$df_line"
    used_pct="${used_pct_raw%%%}"
    total_bytes=$((total_kb * 1024))
    avail_bytes=$((avail_kb * 1024))

    echo "VOLUME path=${VOLUME_PATH} used_pct=${used_pct} free_gib=$(format_gib "$avail_bytes") total_gib=$(format_gib "$total_bytes")"

    if (( used_pct >= CRIT_DISK_PCT )); then
        set_status "CRITICAL" 2 "disk usage is ${used_pct}% on ${VOLUME_PATH}"
    elif (( used_pct >= WARN_DISK_PCT )); then
        set_status "WARNING" 1 "disk usage is ${used_pct}% on ${VOLUME_PATH}"
    fi

    if (( avail_bytes <= CRIT_FREE_BYTES )); then
        set_status "CRITICAL" 2 "free space is $(format_gib "$avail_bytes") GiB on ${VOLUME_PATH}"
    elif (( avail_bytes <= WARN_FREE_BYTES )); then
        set_status "WARNING" 1 "free space is $(format_gib "$avail_bytes") GiB on ${VOLUME_PATH}"
    fi
else
    echo "VOLUME path=${VOLUME_PATH} unavailable=true"
    set_status "CRITICAL" 2 "failed to inspect ${VOLUME_PATH}"
fi

db_bytes=0
if [[ -f "$DB_FILE" ]]; then
    if db_bytes="$(stat -c %s "$DB_FILE" 2>/dev/null)" && [[ "$db_bytes" =~ ^[0-9]+$ ]]; then
        echo "DATABASE file=${DB_FILE} size_gib=$(format_gib "$db_bytes") size_bytes=${db_bytes}"

        if (( db_bytes >= CRIT_DB_BYTES )); then
            set_status "CRITICAL" 2 "database size is $(format_gib "$db_bytes") GiB"
        elif (( db_bytes >= WARN_DB_BYTES )); then
            set_status "WARNING" 1 "database size is $(format_gib "$db_bytes") GiB"
        fi
    else
        echo "DATABASE file=${DB_FILE} stat_failed=true"
        set_status "CRITICAL" 2 "failed to inspect ${DB_FILE}"
    fi
else
    echo "DATABASE file=${DB_FILE} missing=true"
    set_status "WARNING" 1 "database file ${DB_FILE} is missing"
fi

if binance_payload="$(curl -sf --max-time 3 "$BINANCE_HEALTH_URL" 2>/dev/null)"; then
    db_write_fresh="$(echo "$binance_payload" | jq -r '.freshness.db_write_fresh // "unknown"')"
    db_write_age_ms="$(echo "$binance_payload" | jq -r '.freshness.db_write_age_ms // "null"')"
    reported_db_bytes="$(echo "$binance_payload" | jq -r '.storage.database_size_bytes // "null"')"

    echo "BINANCE_HEALTH url=${BINANCE_HEALTH_URL} db_write_fresh=${db_write_fresh} db_write_age_ms=${db_write_age_ms} reported_db_bytes=${reported_db_bytes}"

    if [[ "$db_write_fresh" == "false" ]]; then
        set_status "CRITICAL" 2 "binance /health reports stale DB writes"
    fi
else
    echo "BINANCE_HEALTH url=${BINANCE_HEALTH_URL} unavailable=true"
    set_status "WARNING" 1 "binance /health unavailable while checking storage guardrails"
fi

echo "SUMMARY status=${status}"
if ((${#messages[@]} == 0)); then
    echo "OK: storage thresholds not crossed"
else
    printf '%s\n' "${messages[@]}"
fi

exit "$exit_code"