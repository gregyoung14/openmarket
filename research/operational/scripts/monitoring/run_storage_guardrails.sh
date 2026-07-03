#!/usr/bin/env bash

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK_SCRIPT="$REPO_DIR/scripts/monitoring/check_storage_guardrails.sh"
WEBHOOK_URL="${STORAGE_GUARDRAILS_WEBHOOK_URL:-}"
HOST_NAME="$(hostname -s 2>/dev/null || hostname)"
RUN_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    echo "RUN host=${HOST_NAME} run_at=${RUN_AT} status=CRITICAL error=missing_check_script path=${CHECK_SCRIPT}" >&2
    exit 2
fi

output="$($CHECK_SCRIPT 2>&1)"
exit_code=$?

printf 'RUN host=%s run_at=%s exit_code=%s\n' "$HOST_NAME" "$RUN_AT" "$exit_code"
printf '%s\n' "$output"

summary_line="$(printf '%s\n' "$output" | awk '/^SUMMARY / { print; exit }')"
summary_line="${summary_line:-SUMMARY status=UNKNOWN}"

if (( exit_code != 0 )) && [[ -n "$WEBHOOK_URL" ]]; then
    payload="$(jq -n \
        --arg host "$HOST_NAME" \
        --arg run_at "$RUN_AT" \
        --arg summary "$summary_line" \
        --arg output "$output" \
        --argjson exit_code "$exit_code" \
        '{host: $host, run_at: $run_at, exit_code: $exit_code, summary: $summary, output: $output, text: ("storage guardrails " + $summary + " on " + $host)}')"

    if ! curl -fsS -X POST -H 'Content-Type: application/json' --data "$payload" "$WEBHOOK_URL" >/dev/null; then
        echo "ALERT status=WARNING detail=webhook_delivery_failed url=${WEBHOOK_URL}" >&2
    else
        echo "ALERT status=OK detail=webhook_delivered"
    fi
fi

exit "$exit_code"