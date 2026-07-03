#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAPER_CHECK_SCRIPT="$SCRIPT_DIR/check_paper_tournament.sh"
STORAGE_CHECK_SCRIPT="$SCRIPT_DIR/check_storage_guardrails.sh"
overall_code=0

set_overall_code() {
    local candidate="$1"
    if (( candidate > overall_code )); then
        overall_code="$candidate"
    fi
}

print_header() {
    echo "Service Status Check"
    echo "===================="
}

print_json_service() {
    local label="$1"
    local url="$2"
    local jq_program="$3"
    local payload

    echo
    if payload="$(curl -sf --max-time 3 "$url" 2>/dev/null)"; then
        echo "OK: $label"
        echo "$payload" | jq -r "$jq_program"
        return 0
    else
        echo "DOWN: $label ($url)"
        return 2
    fi
}

print_header

print_json_service \
    "Binance (8001)" \
    "http://127.0.0.1:8001/health" \
    '
        "  status: \(.status)",
        "  trades_stored: \(.trades_stored)",
        "  upstream_trade_age_ms: \(.freshness.upstream_trade_age_ms)",
        "  db_write_age_ms: \(.freshness.db_write_age_ms)",
        "  database_size_bytes: \(.storage.database_size_bytes)"
    '
set_overall_code "$?"

print_json_service \
    "Polymarket (8002)" \
    "http://127.0.0.1:8002/health" \
    '
        "  status: \(.status)",
        "  market: \(.market.current)",
        "  upstream_message_age_ms: \(.freshness.upstream_message_age_ms)",
        "  market_data_age_ms: \(.freshness.market_data_age_ms)"
    '
set_overall_code "$?"

print_json_service \
    "Market Recorder (8005)" \
    "http://127.0.0.1:8005/health" \
    '
        "  status: \(.status)",
        "  service: \(.service)"
    '
set_overall_code "$?"

print_json_service \
    "Redeem Positions (8006)" \
    "http://127.0.0.1:8006/health" \
    '
        "  status: \(.status)",
        "  service: \(.service)"
    '
set_overall_code "$?"

if [[ -x "$PAPER_CHECK_SCRIPT" ]]; then
    echo
    echo "Paper Tournament"
    echo "----------------"
    "$PAPER_CHECK_SCRIPT"
    set_overall_code "$?"
fi

if [[ -x "$STORAGE_CHECK_SCRIPT" ]]; then
    echo
    echo "Storage Guardrails"
    echo "------------------"
    "$STORAGE_CHECK_SCRIPT"
    set_overall_code "$?"
fi

exit "$overall_code"
