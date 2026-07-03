#!/usr/bin/env bash

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARTIFACT_PATH="${CALIBRATED_MODEL_PATH:-$REPO_DIR/data/ml_artifacts/latest_binary_model.json}"
WARN_HOURS="${CALIBRATED_ARTIFACT_WARN_HOURS:-24}"
CRIT_HOURS="${CALIBRATED_ARTIFACT_CRIT_HOURS:-72}"

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

if [[ ! -f "$ARTIFACT_PATH" ]]; then
    echo "ARTIFACT path=${ARTIFACT_PATH} missing=true"
    echo "SUMMARY status=CRITICAL"
    echo "CRITICAL: calibrated artifact missing"
    exit 2
fi

mtime_epoch="$(stat -c %Y "$ARTIFACT_PATH" 2>/dev/null || echo 0)"
age_hours="$(( ( $(date +%s) - mtime_epoch ) / 3600 ))"
artifact_version="$(jq -r '.artifact_version // "unknown"' "$ARTIFACT_PATH" 2>/dev/null || echo unknown)"
generated_at="$(jq -r '.generated_at // "unknown"' "$ARTIFACT_PATH" 2>/dev/null || echo unknown)"
brier="$(jq -r '.metrics.brier // "null"' "$ARTIFACT_PATH" 2>/dev/null || echo null)"
ece="$(jq -r '.metrics.ece // "null"' "$ARTIFACT_PATH" 2>/dev/null || echo null)"

echo "ARTIFACT path=${ARTIFACT_PATH} artifact_version=${artifact_version} generated_at=${generated_at} age_hours=${age_hours} brier=${brier} ece=${ece}"

if (( age_hours >= CRIT_HOURS )); then
    set_status "CRITICAL" 2 "artifact age ${age_hours}h >= critical threshold ${CRIT_HOURS}h"
elif (( age_hours >= WARN_HOURS )); then
    set_status "WARNING" 1 "artifact age ${age_hours}h >= warning threshold ${WARN_HOURS}h"
fi

echo "SUMMARY status=${status}"
if ((${#messages[@]} == 0)); then
    echo "OK: calibrated artifact freshness within threshold"
else
    printf '%s\n' "${messages[@]}"
fi

exit "$exit_code"
