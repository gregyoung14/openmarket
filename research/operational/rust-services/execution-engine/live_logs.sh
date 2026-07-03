#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/../../logs/execution-engine.log"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

echo "Streaming $LOG_FILE"
echo "Press Ctrl+C to stop"

tail -n 200 -F "$LOG_FILE"
