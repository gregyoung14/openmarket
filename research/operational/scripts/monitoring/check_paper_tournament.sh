#!/usr/bin/env bash

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCHER="$REPO_DIR/services/paper-tournament/start_paper_tournament.sh"
PID_DIR="${PAPER_PID_DIR:-/var/lib/polymarket/paper_logs/.pids}"
LOG_DIR="${PAPER_LOG_DIR:-/var/lib/polymarket/paper_logs}"
STORAGE_CHECK_SCRIPT="$REPO_DIR/scripts/monitoring/check_storage_guardrails.sh"
ARTIFACT_CHECK_SCRIPT="$REPO_DIR/scripts/monitoring/check_calibrated_artifact.sh"
BINANCE_HEALTH_URL="${PAPER_BINANCE_HEALTH_URL:-http://127.0.0.1:8001/health}"
POLYMARKET_HEALTH_URL="${PAPER_POLYMARKET_HEALTH_URL:-http://127.0.0.1:8002/health}"

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

strategy_names=()
signal_ports=()
executor_ports=()

while IFS= read -r line; do
    entry="${line#*\"}"
    entry="${entry%%\"*}"
    IFS='|' read -r name sig_port exec_port _ <<< "$entry"
    strategy_names+=("$name")
    signal_ports+=("$sig_port")
    executor_ports+=("$exec_port")
done < <(grep -E '^[[:space:]]*".*\|8[0-9]{3}\|9[0-9]{3}\|' "$LAUNCHER")

if ((${#strategy_names[@]} == 0)); then
    echo "SUMMARY status=CRITICAL"
    echo "CRITICAL: failed to parse tournament strategies from ${LAUNCHER}"
    exit 2
fi

if ! binance_payload="$(curl -sf --max-time 3 "$BINANCE_HEALTH_URL" 2>/dev/null)"; then
    set_status "CRITICAL" 2 "binance /health is unavailable"
else
    binance_status="$(echo "$binance_payload" | jq -r '.status')"
    binance_trade_fresh="$(echo "$binance_payload" | jq -r '.freshness.upstream_trade_fresh // "unknown"')"
    binance_db_fresh="$(echo "$binance_payload" | jq -r '.freshness.db_write_fresh // "unknown"')"
    echo "UPSTREAM service=binance status=${binance_status} upstream_trade_fresh=${binance_trade_fresh} db_write_fresh=${binance_db_fresh} trade_age_ms=$(echo "$binance_payload" | jq -r '.freshness.upstream_trade_age_ms // "null"') db_write_age_ms=$(echo "$binance_payload" | jq -r '.freshness.db_write_age_ms // "null"')"

    if [[ "$binance_status" != "ok" || "$binance_trade_fresh" != "true" || "$binance_db_fresh" != "true" ]]; then
        set_status "CRITICAL" 2 "binance upstream or DB freshness check failed"
    fi
fi

if ! polymarket_payload="$(curl -sf --max-time 3 "$POLYMARKET_HEALTH_URL" 2>/dev/null)"; then
    set_status "CRITICAL" 2 "polymarket /health is unavailable"
else
    polymarket_status="$(echo "$polymarket_payload" | jq -r '.status')"
    polymarket_message_fresh="$(echo "$polymarket_payload" | jq -r '.freshness.upstream_message_fresh // "unknown"')"
    polymarket_data_fresh="$(echo "$polymarket_payload" | jq -r '.freshness.market_data_fresh // "unknown"')"
    echo "UPSTREAM service=polymarket status=${polymarket_status} market=$(echo "$polymarket_payload" | jq -r '.market.current // "unknown"') upstream_message_fresh=${polymarket_message_fresh} market_data_fresh=${polymarket_data_fresh} market_data_age_ms=$(echo "$polymarket_payload" | jq -r '.freshness.market_data_age_ms // "null"')"

    if [[ "$polymarket_status" != "ok" || "$polymarket_message_fresh" != "true" || "$polymarket_data_fresh" != "true" ]]; then
        set_status "CRITICAL" 2 "polymarket upstream freshness check failed"
    fi
fi

expected_market=""

for index in "${!strategy_names[@]}"; do
    name="${strategy_names[$index]}"
    signal_port="${signal_ports[$index]}"
    executor_port="${executor_ports[$index]}"

    signal_pid_file="$PID_DIR/signal_${name}.pid"
    executor_pid_file="$PID_DIR/executor_${name}.pid"

    if [[ -f "$signal_pid_file" ]]; then
        signal_pid="$(cat "$signal_pid_file")"
        if ! kill -0 "$signal_pid" 2>/dev/null; then
            set_status "CRITICAL" 2 "signal process for ${name} is not running"
        fi
    else
        set_status "CRITICAL" 2 "missing signal pid file for ${name}"
        signal_pid="missing"
    fi

    if [[ -f "$executor_pid_file" ]]; then
        executor_pid="$(cat "$executor_pid_file")"
        if ! kill -0 "$executor_pid" 2>/dev/null; then
            set_status "CRITICAL" 2 "executor process for ${name} is not running"
        fi
    else
        set_status "CRITICAL" 2 "missing executor pid file for ${name}"
        executor_pid="missing"
    fi

    if ! payload="$(curl -sf --max-time 4 "http://127.0.0.1:${signal_port}/health" 2>/dev/null)"; then
        echo "STRATEGY name=${name} signal_port=${signal_port} executor_port=${executor_port} signal_pid=${signal_pid} executor_pid=${executor_pid} health=unavailable"
        set_status "CRITICAL" 2 "signal /health unavailable for ${name} on ${signal_port}"
        continue
    fi

    signal_status="$(echo "$payload" | jq -r '.status')"
    market="$(echo "$payload" | jq -r '.market.current // "unknown"')"
    binance_fresh="$(echo "$payload" | jq -r '.freshness.binance_trade_fresh // "unknown"')"
    polymarket_fresh="$(echo "$payload" | jq -r '.freshness.polymarket_data_fresh // "unknown"')"
    calibrated_mode="$(echo "$payload" | jq -r '.calibrated.mode // "disabled"')"
    calibrated_loaded="$(echo "$payload" | jq -r '.calibrated.loaded // "false"')"
    calibrated_artifact="$(echo "$payload" | jq -r '.calibrated.artifact_version // "none"')"
    calibrated_prob_up="$(echo "$payload" | jq -r '.calibrated.last_prob_up // "null"')"
    calibrated_ev_up="$(echo "$payload" | jq -r '.calibrated.last_ev_up // "null"')"
    calibrated_ev_down="$(echo "$payload" | jq -r '.calibrated.last_ev_down // "null"')"
    entries_fired="$(echo "$payload" | jq -r '.counters.entries_fired // 0')"

    echo "STRATEGY name=${name} signal_port=${signal_port} executor_port=${executor_port} signal_pid=${signal_pid} executor_pid=${executor_pid} status=${signal_status} market=${market} entries_fired=${entries_fired} binance_fresh=${binance_fresh} polymarket_fresh=${polymarket_fresh} calibrated_mode=${calibrated_mode} calibrated_loaded=${calibrated_loaded} calibrated_artifact=${calibrated_artifact} calibrated_prob_up=${calibrated_prob_up} calibrated_ev_up=${calibrated_ev_up} calibrated_ev_down=${calibrated_ev_down}"

    if [[ -z "$expected_market" ]]; then
        expected_market="$market"
    elif [[ "$market" != "$expected_market" ]]; then
        set_status "CRITICAL" 2 "market mismatch across tournament ports (${name}=${market}, expected=${expected_market})"
    fi

    if [[ "$signal_status" != "ok" || "$binance_fresh" != "true" || "$polymarket_fresh" != "true" ]]; then
        set_status "CRITICAL" 2 "freshness check failed for ${name}"
    fi
    if [[ "$calibrated_mode" != "disabled" && "$calibrated_loaded" != "true" ]]; then
        set_status "WARNING" 1 "calibrated scorer requested but not loaded for ${name}"
    fi
done

latest_csv="$(find "$LOG_DIR" -maxdepth 1 -name 'paper_log_*.csv' -printf '%T@ %f\n' 2>/dev/null | sort -nr | head -n 1)"
if [[ -n "$latest_csv" ]]; then
    latest_ts="${latest_csv%% *}"
    latest_file="${latest_csv#* }"
    latest_age_secs=$(( $(date +%s) - ${latest_ts%.*} ))
    echo "CSV_ACTIVITY latest_file=${latest_file} latest_age_secs=${latest_age_secs}"
else
    echo "CSV_ACTIVITY latest_file=none latest_age_secs=unknown"
    set_status "WARNING" 1 "no paper_log_*.csv files found in ${LOG_DIR}"
fi

if [[ -x "$STORAGE_CHECK_SCRIPT" ]]; then
    storage_output="$($STORAGE_CHECK_SCRIPT 2>&1)"
    storage_code=$?

    while IFS= read -r line; do
        [[ -n "$line" ]] && echo "STORAGE ${line}"
    done <<< "$storage_output"

    if (( storage_code == 2 )); then
        set_status "CRITICAL" 2 "storage guardrail script reported CRITICAL"
    elif (( storage_code == 1 )); then
        set_status "WARNING" 1 "storage guardrail script reported WARNING"
    fi
else
    set_status "WARNING" 1 "storage guardrail script is missing or not executable"
fi

if [[ -x "$ARTIFACT_CHECK_SCRIPT" ]]; then
    artifact_output="$(
        CALIBRATED_ARTIFACT_WARN_HOURS="${CALIBRATED_ARTIFACT_WARN_HOURS:-720}" \
        CALIBRATED_ARTIFACT_CRIT_HOURS="${CALIBRATED_ARTIFACT_CRIT_HOURS:-1440}" \
        "$ARTIFACT_CHECK_SCRIPT" 2>&1
    )"
    artifact_code=$?

    while IFS= read -r line; do
        [[ -n "$line" ]] && echo "ARTIFACT_CHECK ${line}"
    done <<< "$artifact_output"

    if (( artifact_code == 2 )); then
        set_status "CRITICAL" 2 "calibrated artifact check reported CRITICAL"
    elif (( artifact_code == 1 )); then
        set_status "WARNING" 1 "calibrated artifact check reported WARNING"
    fi
else
    set_status "WARNING" 1 "calibrated artifact check script is missing or not executable"
fi

echo "SUMMARY status=${status} strategies=${#strategy_names[@]} market=${expected_market}"
if ((${#messages[@]} == 0)); then
    echo "OK: paper tournament checks passed"
else
    printf '%s\n' "${messages[@]}"
fi

exit "$exit_code"